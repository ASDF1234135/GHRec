"""Daily feature tables, point-in-time correct: every number uses only events with ts < asof.
Usage: python -m ghrec.features 2026-11-10 --root data   -> <root>/features/asof=2026-11-10/{repo,user}_features.parquet
(Language/topic features come from the as-of metadata history in ghrec.pipeline, not from here.)"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl


def _win(asof: datetime, days: int) -> pl.Expr:
    return (pl.col("ts") >= asof - timedelta(days=days)) & (pl.col("ts") < asof)


def repo_features(ev: pl.DataFrame, asof: datetime) -> pl.DataFrame:
    ev = ev.filter(pl.col("ts") < asof)  # nothing at or after asof may leak in
    star = pl.col("type") == "WatchEvent"
    return ev.group_by("repo").agg(
        (star & _win(asof, 1)).sum().alias("stars_1d"),
        (star & _win(asof, 7)).sum().alias("stars_7d"),
        (star & _win(asof, 30)).sum().alias("stars_30d"),
        _win(asof, 30).sum().alias("events_30d"),
        pl.col("user").filter(_win(asof, 30)).n_unique().alias("users_30d"),
    ).sort("repo")


def user_features(ev: pl.DataFrame, asof: datetime) -> pl.DataFrame:
    ev = ev.filter(pl.col("ts") < asof)
    return ev.group_by("user").agg(
        _win(asof, 7).sum().alias("events_7d"),
        _win(asof, 30).sum().alias("events_30d"),
        pl.col("repo").filter(_win(asof, 30)).n_unique().alias("repos_30d"),
        ((pl.col("type") == "WatchEvent") & _win(asof, 30)).sum().alias("stars_30d"),
    ).sort("user")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("asof"); ap.add_argument("--root", default="data")
    a = ap.parse_args()
    asof, root = datetime.fromisoformat(a.asof), Path(a.root)
    ev = pl.scan_parquet(root / "raw_events/date=*/events.parquet").filter(pl.col("ts") >= asof - timedelta(days=30)).collect()
    out = root / f"features/asof={a.asof}"
    out.mkdir(parents=True, exist_ok=True)
    repo_features(ev, asof).write_parquet(out / "repo_features.parquet")
    user_features(ev, asof).write_parquet(out / "user_features.parquet")
    print("wrote", out)
