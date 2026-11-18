"""Retrievers: fit(df[u,i,w,t], n_items); retrieve(history_idx, k) -> {item: score}. Seen items are masked."""
import numpy as np
import scipy.sparse as sp


def _top(s: np.ndarray, hist, k) -> dict[int, float]:
    s = s.copy(); s[list(hist)] = 0
    idx = np.flatnonzero(s > 0)
    idx = idx[np.argsort(-s[idx])[:k]]
    return {int(i): float(s[i]) for i in idx}


class Trending:
    name = "trending"

    def fit(self, df, n_items):
        recent = df.filter(df["t"] >= df["t"].max() - 7)
        recent = recent if recent.height else df
        self.s = np.bincount(recent["i"].to_numpy(), recent["w"].to_numpy(), n_items).astype(float)

    def retrieve(self, hist, k):
        return _top(self.s, hist, k)


class ItemKNN:
    name = "item_knn"

    def fit(self, df, n_items):
        u = df["u"].to_numpy()
        X = sp.csr_matrix((np.ones(len(u)), (u, df["i"].to_numpy())), shape=(u.max() + 1, n_items))
        X.data[:] = 1  # duplicates -> binary
        C = (X.T @ X).tocsr(); C.setdiag(0); C.eliminate_zeros()
        d = sp.diags(1 / np.sqrt(np.maximum(np.asarray(X.sum(0)).ravel(), 1)))
        # shortcut: full cosine matrix kept in memory; truncate to top-N neighbours per item at million-repo scale
        self.S = (d @ C @ d).tocsr()

    def retrieve(self, hist, k):
        if not hist:
            return {}
        return _top(np.asarray(self.S[list(hist)].sum(0)).ravel(), hist, k)
