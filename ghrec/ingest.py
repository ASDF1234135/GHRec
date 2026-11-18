"""Daily GH Archive ingest: download 24 hourly files, keep interaction events, QA, write parquet + manifest.
Usage: python -m ghrec.ingest START [END] --root data   (dates YYYY-MM-DD, inclusive; same-day rerun overwrites)"""
import argparse, glob, json, shutil, tempfile, urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

URL = "https://data.gharchive.org"
COLS = ("{'type': 'VARCHAR', 'actor': 'STRUCT(login VARCHAR)', 'repo': 'STRUCT(name VARCHAR)', "
        "'created_at': 'VARCHAR', 'payload': 'STRUCT(action VARCHAR)'}")
# Keep real engagement only: opened issues/PRs (not closes/merges by maintainers), new comments and reviews.
KEEP = """(type IN ('WatchEvent', 'ForkEvent')
   OR (type IN ('IssuesEvent', 'PullRequestEvent') AND payload.action = 'opened')
   OR (type IN ('IssueCommentEvent', 'PullRequestReviewCommentEvent', 'PullRequestReviewEvent') AND payload.action = 'created'))"""


def extract(files: list[str]) -> pl.DataFrame:
    sql = f"""SELECT actor.login AS "user", repo.name AS repo, type, created_at AS ts
              FROM read_json(?, columns={COLS}, format='newline_delimited', ignore_errors=true)
              WHERE {KEEP}"""
    df = pl.from_arrow(duckdb.connect().execute(sql, [files]).to_arrow_table())
    return df.with_columns(pl.col("ts").str.slice(0, 19).str.to_datetime("%Y-%m-%dT%H:%M:%S"))


def check(df: pl.DataFrame, day: date, root: Path) -> dict:
    n = df.height
    nulls = df.select(pl.col("user").is_null() | pl.col("repo").is_null()).to_series().mean() if n else 1.0
    prev = [json.loads(Path(p).read_text())["rows"] for p in sorted(glob.glob(str(root / "raw_events/date=*/manifest.json")))
            if Path(p).parent.name != f"date={day}"][-7:]
    avg = sum(prev) / len(prev) if prev else None
    if n == 0 or nulls > 0.01 or (avg and not 0.5 * avg <= n <= 2 * avg):
        raise ValueError(f"quality check failed for {day}: rows={n}, null_rate={nulls}, 7d_avg={avg}")
    return {"rows": n, "null_rate": nulls, "avg_7d": avg}


def ingest_day(day: date, root: Path) -> None:
    out = root / f"raw_events/date={day}"
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for h in range(24):  # a missing hour raises HTTPError -> job fails -> alert
            p = f"{tmp}/{h}.json.gz"
            req = urllib.request.Request(f"{URL}/{day}-{h}.json.gz", headers={"User-Agent": "ghrec-ingest"})  # default UA gets 403
            with urllib.request.urlopen(req, timeout=120) as r, open(p, "wb") as f:
                shutil.copyfileobj(r, f)
            files.append(p)
        df = extract(files)
    qa = check(df, day, root)
    out.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out / "events.parquet")
    (out / "manifest.json").write_text(json.dumps({**qa, "processed_at": datetime.utcnow().isoformat()}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("start"); ap.add_argument("end", nargs="?"); ap.add_argument("--root", default="data")
    a = ap.parse_args()
    d, end = date.fromisoformat(a.start), date.fromisoformat(a.end or a.start)
    while d <= end:
        ingest_day(d, Path(a.root)); print("ingested", d); d += timedelta(days=1)
