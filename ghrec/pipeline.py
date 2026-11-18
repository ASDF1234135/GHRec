"""Three-stage pipeline: multi-route recall -> LightGBM LambdaRank -> rule rerank."""
import pickle

import numpy as np
from lightgbm import LGBMRanker

from .rerank import owner_dedup
from .retrieval import ItemKNN, Trending


class Pipeline:
    def __init__(self, repos: list[str], quotas=None):
        self.repos, self.r2i = repos, {r: i for i, r in enumerate(repos)}
        self.quotas = quotas or {"item_knn": 300, "trending": 100}
        self.retrievers = [ItemKNN(), Trending()]
        self.ranker = None
        n = len(repos)  # repo metadata (see set_meta); zeros/empty when not enriched
        self.lang, self.topics, self.age, self.has_meta = np.full(n, -1), [frozenset()] * n, np.zeros(n), np.zeros(n)

    def set_meta(self, meta, T, asof=None):
        """meta: ghrec.enrich history. created_at never changes, so age is always safe; language and topics do
        change, so only fetches made at or before `asof` (default T) are used. Star counts are never used."""
        asof, langs = asof or T, {}
        for r in meta.sort("fetched_at").iter_rows(named=True):
            i = self.r2i.get(r["repo"])
            if i is None or r["created_at"] is None:
                continue
            self.age[i], self.has_meta[i] = max(0, (T - r["created_at"]).days) / 365, 1
            if r["fetched_at"] <= asof:
                self.lang[i] = langs.setdefault(r["language"], len(langs)) if r["language"] else -1
                self.topics[i] = frozenset(r["topics"] or [])

    def hist_idx(self, names) -> set[int]:
        return {self.r2i[n] for n in names if n in self.r2i}  # unknown repos are ignored

    def candidates(self, hist) -> dict:
        c = {}
        for r in self.retrievers:
            sc = sorted(r.retrieve(hist, self.quotas[r.name]).items(), key=lambda x: -x[1])
            for rank, (i, s) in enumerate(sc):
                c.setdefault(i, {})[r.name] = (s, rank)
        return c

    def _feats(self, c, hist):
        ids = list(c)
        hl = [self.lang[h] for h in hist if self.lang[h] >= 0]
        ht = set().union(*(self.topics[h] for h in hist)) if hist else set()
        X = [[v for r in self.retrievers for v in c[i].get(r.name, (0.0, self.quotas[r.name]))] + [self.pop[i]]
             + [hl.count(self.lang[i]) / len(hl) if hl and self.lang[i] >= 0 else 0.0,  # language match
                len(self.topics[i] & ht) / max(1, len(self.topics[i])),                  # topic overlap
                self.age[i], self.has_meta[i]] for i in ids]
        return ids, np.array(X)

    def fit(self, retr, rank):
        n = len(self.repos)
        for r in self.retrievers:
            r.fit(retr, n)
        self.pop = np.log1p(np.bincount(retr["i"].to_numpy(), minlength=n))
        hist = {u: set(g["i"]) for (u,), g in retr.group_by("u")}
        Xs, ys, groups = [], [], []
        for (u,), g in rank.group_by("u"):
            if u not in hist:
                continue
            pos = set(g["i"])
            ids, X = self._feats(self.candidates(hist[u]), hist[u])
            y = [int(i in pos) for i in ids]
            if 0 < sum(y) < len(y):
                Xs.append(X); ys += y; groups.append(len(y))
        if not groups:
            raise ValueError("no ranker training groups")
        self.ranker = LGBMRanker(objective="lambdarank", n_estimators=100, min_child_samples=5, verbose=-1)
        self.ranker.fit(np.vstack(Xs), ys, group=groups)

    def recommend(self, names, k=10) -> list[dict]:
        hi = self.hist_idx(names)
        c = self.candidates(hi)
        if not c:
            return []
        ids, X = self._feats(c, hi)
        s = self.ranker.predict(X)
        scores = dict(zip(ids, s))
        top = owner_dedup([ids[j] for j in np.argsort(-s)], self.repos, k)
        return [{"repo": self.repos[i], "score": float(scores[i]), "sources": list(c[i])} for i in top]

    def save(self, path):
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path) -> "Pipeline":
        with open(path, "rb") as f:  # internal artifact only; never load untrusted pickles
            return pickle.load(f)
