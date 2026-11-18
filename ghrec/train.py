"""Build snapshot, train challenger, evaluate challenger vs champion on the same snapshot, promote via gate.
Usage: python -m ghrec.train [--root data | --synthetic] [--T 2026-11-08] [--artifacts artifacts]"""
import argparse, json, os
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from .data import SPANS, clean, prepare, snapshot_version, split, synthetic
from .eval import evaluate
from .gate import gate
from .pipeline import Pipeline


def run(df: pl.DataFrame, T: datetime, artifacts: Path, prod_json: Path | None = None,
        spans=SPANS, k=(5, 10), snapshots: Path | None = None, meta: pl.DataFrame | None = None) -> dict:
    df = clean(df, *k, cutoff=T - timedelta(days=spans[3]))
    version = snapshot_version(df, T)
    df, repos = prepare(df, T)
    seg = split(df, T, spans)
    if snapshots:
        out = snapshots / version
        out.mkdir(parents=True, exist_ok=True)
        for name, part in seg.items():
            part.write_parquet(out / f"{name}.parquet")
        (out / "manifest.json").write_text(json.dumps({"version": version, "T": T.isoformat(), "spans": spans,
                                                       "k_core": k, "rows": {n: p.height for n, p in seg.items()}}))
    chal = Pipeline(repos)
    if meta is not None:
        chal.set_meta(meta, T, asof=T - timedelta(days=spans[1]))  # ranker-segment start
    chal.fit(seg["retr"], seg["rank"])
    hist = pl.concat([seg["retr"], seg["rank"], seg["valid"]])
    m = evaluate(chal, hist, seg["test"])
    champ_path, cm = artifacts / "champion.pkl", None
    if champ_path.exists():
        cm = evaluate(Pipeline.load(champ_path), hist, seg["test"])
    ok, why = gate(m, cm)
    if ok:
        artifacts.mkdir(parents=True, exist_ok=True)
        chal.save(champ_path)
        if prod_json:
            prod_json.parent.mkdir(parents=True, exist_ok=True)
            prod_json.write_text(json.dumps({"snapshot": T.date().isoformat(), "model": str(champ_path), "metrics": m}, indent=2))
    res = {"snapshot": version, "n_repos": len(repos), "rows": {n: p.height for n, p in seg.items()}, "promoted": ok, "reason": why, "challenger": m, "champion": cm}
    if os.environ.get("MLFLOW_TRACKING_URI"):
        _log_mlflow(res, T, spans, k, meta is not None, champ_path if ok else None)
    return res


def _log_mlflow(res: dict, T: datetime, spans, k, with_meta: bool, model_path: Path | None) -> None:
    """One run per training; the `snapshot` tag lets the leaderboard compare only runs on the same snapshot."""
    import mlflow  # optional dependency: pip install ".[mlflow]"

    mlflow.set_experiment("ghrec")
    with mlflow.start_run(run_name=res["snapshot"]):
        mlflow.set_tags({"snapshot": res["snapshot"], "promoted": str(res["promoted"]), "gate_reason": res["reason"]})
        mlflow.log_params({"T": T.date().isoformat(), "spans": list(spans), "k_core": list(k), "with_meta": with_meta})
        name = lambda prefix, key: f"{prefix}_{key.replace('@', '_at_')}"  # '@' is not allowed in metric names
        mlflow.log_metrics({name("challenger", n): v for n, v in res["challenger"].items()})
        if res["champion"]:
            mlflow.log_metrics({name("champion", n): v for n, v in res["champion"].items()})
        if model_path:
            mlflow.log_artifact(str(model_path))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data"); ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--T", default=datetime.utcnow().date().isoformat()); ap.add_argument("--artifacts", default="artifacts")
    ap.add_argument("--spans", default="90,21,14,7", help="days before T where retr,rank,valid,test start")
    ap.add_argument("--k", default="5,10", help="k-core: min user, min repo interactions")
    ap.add_argument("--no-meta", action="store_true", help="ignore repo_meta.parquet (for A/B comparison)")
    a = ap.parse_args()
    T = datetime.fromisoformat(a.T)
    mp = Path(a.root) / "repo_meta/repo_meta.parquet"
    meta = pl.read_parquet(mp) if mp.exists() and not a.synthetic and not a.no_meta else None
    df = synthetic(T) if a.synthetic else pl.read_parquet(f"{a.root}/raw_events/date=*/events.parquet")
    res = run(df, T, Path(a.artifacts), Path("deploy/production.json"), tuple(map(int, a.spans.split(","))),
              tuple(map(int, a.k.split(","))), Path("snapshots"), meta)
    print(json.dumps(res, indent=2))
