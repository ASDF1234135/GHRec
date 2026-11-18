"""Cleaning, indexing, time-based snapshot split, and a synthetic generator for tests/smoke runs."""
import hashlib
from datetime import datetime, timedelta

import numpy as np
import polars as pl

W = {"WatchEvent": 1, "ForkEvent": 2, "IssueCommentEvent": 2, "IssuesEvent": 3, "PullRequestEvent": 3,
     "PullRequestReviewEvent": 3, "PullRequestReviewCommentEvent": 3}


def _kcore(df: pl.DataFrame, k_user: int, k_repo: int) -> pl.DataFrame:
    while True:
        n = df.height
        df = df.filter(pl.len().over("user") >= k_user).filter(pl.len().over("repo") >= k_repo)
        if df.height == n:
            return df


def clean(df: pl.DataFrame, k_user=5, k_repo=10, cutoff: datetime | None = None) -> pl.DataFrame:
    """cutoff = start of the test segment: k-core is computed on earlier rows only, so test-period activity
    never decides who survives. Later rows are kept only for users and repos that survive (known to the model)."""
    df = df.filter(~pl.col("user").str.ends_with("[bot]"))
    df = df.filter(pl.col("repo").str.split("/").list.get(0) != pl.col("user"))  # own repos
    df = df.sort("ts").unique(["user", "repo", "type"], keep="first", maintain_order=True)
    df = df.with_columns(w=pl.col("type").replace_strict(W, return_dtype=pl.Int64))
    df = df.group_by(["user", "repo"]).agg(pl.col("w").max(), pl.col("ts").min())  # one row per pair
    if cutoff is None:
        return _kcore(df, k_user, k_repo)
    core = _kcore(df.filter(pl.col("ts") < cutoff), k_user, k_repo)
    later = (df.filter(pl.col("ts") >= cutoff)
             .join(core.select("user").unique(), on="user", how="semi")
             .join(core.select("repo").unique(), on="repo", how="semi"))
    return pl.concat([core, later])


def prepare(df: pl.DataFrame, T: datetime):
    """Add integer ids u,i and t (days relative to T). Returns (df, repos)."""
    repos = sorted(df["repo"].unique().to_list())
    users = sorted(df["user"].unique().to_list())
    df = df.with_columns(
        u=pl.col("user").replace_strict({x: j for j, x in enumerate(users)}, return_dtype=pl.Int64),
        i=pl.col("repo").replace_strict({x: j for j, x in enumerate(repos)}, return_dtype=pl.Int64),
        t=(pl.col("ts") - T).dt.total_seconds() / 86400,
    )
    return df, repos


SPANS = (90, 21, 14, 7)  # days before T at which retr / rank / valid / test start


def split(df: pl.DataFrame, T: datetime, spans=SPANS) -> dict[str, pl.DataFrame]:
    """Default segments: retr [T-90,T-21), rank [T-21,T-14), valid [T-14,T-7), test [T-7,T). Half-open, disjoint."""
    d = lambda n: T - timedelta(days=n)
    seg = lambda a, b: df.filter((pl.col("ts") >= d(a)) & (pl.col("ts") < d(b)))
    a, b, c, e = spans
    return {"retr": seg(a, b), "rank": seg(b, c), "valid": seg(c, e), "test": seg(e, 0)}


def snapshot_version(df: pl.DataFrame, T: datetime) -> str:
    """<base date>-<hash of the cleaned data>; row order does not matter."""
    h = hashlib.sha256(df.select("user", "repo", "w", "ts").sort("user", "repo").write_csv().encode()).hexdigest()[:8]
    return f"{T.date()}-{h}"


def synthetic(T: datetime, n_users=300, n_repos=200, n_clusters=5, per_user=40, seed=0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rc = rng.integers(0, n_clusters, n_repos)
    rows = []
    for u in range(n_users):
        c = rng.integers(0, n_clusters)
        for _ in range(per_user):
            pool = np.flatnonzero(rc == c) if rng.random() < 0.85 else np.arange(n_repos)
            r = rng.choice(pool)
            ts = T - timedelta(days=float(rng.uniform(0, 89.9)))
            rows.append((f"user{u}", f"owner{r % 20}/repo{r}", str(rng.choice(["WatchEvent", "ForkEvent", "IssuesEvent", "PullRequestEvent"], p=[.8, .1, .05, .05])), ts))
    return pl.DataFrame(rows, schema=["user", "repo", "type", "ts"], orient="row")
