"""Repo metadata enrichment via the GitHub GraphQL API.
Usage: GITHUB_TOKEN=... python -m ghrec.enrich --root data [--min-events 3] [--limit 5000]
Writes <root>/repo_meta/repo_meta.parquet as an append-only history (one row per fetch), so training can look up
what was known at a past date. Repos fetched within 7 days are skipped; on a rate limit the job saves
what it has and stops, so the next run resumes. Missing/deleted repos are cached as null rows (not refetched)."""
import argparse, json, os, re, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

API = "https://api.github.com/graphql"
NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")  # also keeps untrusted names out of the query text
BATCH, TTL_DAYS = 50, 7
FIELDS = ('primaryLanguage { name } repositoryTopics(first: 10) { nodes { topic { name } } } description createdAt '
          'readme: object(expression: "HEAD:README.md") { ... on Blob { text } }')
SCHEMA = {"repo": pl.String, "language": pl.String, "topics": pl.List(pl.String), "description": pl.String,
          "created_at": pl.Datetime("us"), "readme_head": pl.String, "fetched_at": pl.Datetime("us")}


class RateLimited(Exception):
    pass


def post(query: str) -> dict:
    token = os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(API, json.dumps({"query": query}).encode(), {
        "Authorization": f"bearer {token}", "User-Agent": "ghrec-enrich", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 502, 503):
            raise RateLimited(str(e))
        raise


def _fetch(chunk: list[str], post_fn, now) -> tuple[list[dict], int | None]:
    body = " ".join(f"r{j}: repository(owner: {json.dumps(r.split('/')[0])}, name: {json.dumps(r.split('/')[1])}) {{ {FIELDS} }}"
                    for j, r in enumerate(chunk))
    resp = post_fn(f"query {{ rateLimit {{ remaining }} {body} }}")
    errs = resp.get("errors") or []
    if any(e.get("type") == "RATE_LIMITED" for e in errs):
        raise RateLimited("rate limited")
    data = resp.get("data")
    if data is None:
        raise RuntimeError(f"GraphQL error: {errs}")
    rows = []
    for j, repo in enumerate(chunk):
        n = data.get(f"r{j}")  # None = missing/renamed/private repo (NOT_FOUND error is expected, ignore)
        row = dict.fromkeys(SCHEMA)
        row.update(repo=repo, fetched_at=now)
        if n:
            row.update(
                language=(n.get("primaryLanguage") or {}).get("name"),
                topics=[t["topic"]["name"] for t in n["repositoryTopics"]["nodes"]],
                description=n.get("description"),
                created_at=datetime.fromisoformat(n["createdAt"].replace("Z", "+00:00")).replace(tzinfo=None),
                readme_head=((n.get("readme") or {}).get("text") or "")[:500])
        rows.append(row)
    return rows, (data.get("rateLimit") or {}).get("remaining")


def _save(old: pl.DataFrame, rows: list[dict], path: Path) -> None:
    new = pl.DataFrame(rows, schema=SCHEMA)
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.concat([old, new]).write_parquet(path)  # append: keep earlier fetches for as-of lookups


def enrich(repos: list[str], path: Path, post_fn=post, now: datetime | None = None, limit: int | None = None,
           batch: int = BATCH) -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    old = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=SCHEMA)
    last = old.group_by("repo").agg(pl.col("fetched_at").max())
    fresh = set(last.filter(pl.col("fetched_at") > now - timedelta(days=TTL_DAYS))["repo"])
    todo = [r for r in dict.fromkeys(repos) if r not in fresh and NAME.match(r)][:limit]
    done = 0
    try:
        for i in range(0, len(todo), batch):
            rows, remaining = _fetch(todo[i:i + batch], post_fn, now)
            _save(pl.read_parquet(path) if path.exists() else old, rows, path)  # save per batch = resume point
            done += len(rows)
            if remaining is not None and remaining < batch:
                break
    except RateLimited:
        pass  # progress is already saved after every batch
    return {"requested": len(todo), "fetched": done, "remaining": len(todo) - done}


def select_repos(root: Path, min_events: int) -> list[str]:
    ev = pl.read_parquet(root / "raw_events/date=*/events.parquet")
    return ev.group_by("repo").len().filter(pl.col("len") >= min_events).sort("len", descending=True)["repo"].to_list()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data"); ap.add_argument("--min-events", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--snapshot", help="snapshot dir: enrich only the repos the model actually uses")
    a = ap.parse_args()
    root = Path(a.root)
    repos = (pl.concat([pl.read_parquet(f) for f in Path(a.snapshot).glob("*.parquet")])["repo"].unique().to_list()
             if a.snapshot else select_repos(root, a.min_events))
    print(enrich(repos, root / "repo_meta/repo_meta.parquet", limit=a.limit))
