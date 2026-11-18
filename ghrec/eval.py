import time

import numpy as np


def _ndcg(ranked, rel_n, rel, k=10) -> float:
    dcg = sum(1 / np.log2(r + 2) for r, x in enumerate(ranked[:k]) if x in rel)
    return dcg / sum(1 / np.log2(r + 2) for r in range(min(rel_n, k)))


def evaluate(pipe, hist_df, test_df) -> dict:
    """hist_df/test_df have user, repo columns. Relevant = test repos not already in history.
    Repos unknown to the model still count as misses, so champion/challenger are compared fairly."""
    hist = {u: set(g["repo"]) for (u,), g in hist_df.group_by("user")}
    out = {"e2e": [], "pop": [], "knn": [], "recall": []}
    lat = []
    knn, trend = pipe.retrievers
    for (u,), g in test_df.group_by("user"):
        h = hist.get(u)
        rel = set(g["repo"]) - (h or set())
        if not h or not rel:
            continue
        t0 = time.perf_counter()
        recs = [r["repo"] for r in pipe.recommend(h, 10)]
        lat.append((time.perf_counter() - t0) * 1000)
        hi = pipe.hist_idx(h)
        top = lambda r: [pipe.repos[i] for i in r.retrieve(hi, 10)]  # dict order = score order
        out["e2e"].append(_ndcg(recs, len(rel), rel))
        out["knn"].append(_ndcg(top(knn), len(rel), rel))
        out["pop"].append(_ndcg(top(trend), len(rel), rel))
        out["recall"].append(len({pipe.repos[i] for i in pipe.candidates(hi)} & rel) / len(rel))
    m = lambda k: float(np.mean(out[k])) if out[k] else 0.0
    return {"e2e_ndcg@10": m("e2e"), "popularity_ndcg@10": m("pop"), "item_knn_ndcg@10": m("knn"),
            "candidate_recall": m("recall"), "n_users": len(lat),
            "p99_ms": float(np.percentile(lat, 99)) if lat else 0.0}
