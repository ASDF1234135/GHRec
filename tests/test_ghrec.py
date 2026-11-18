import gzip, json
from datetime import date
from datetime import datetime, timedelta

import numpy as np

import polars as pl
import pytest
from fastapi.testclient import TestClient

from ghrec.data import clean, prepare, snapshot_version, split, synthetic
from ghrec.pipeline import Pipeline
from ghrec import s3sync
from ghrec.features import repo_features, user_features
from ghrec.enrich import RateLimited, enrich
from ghrec.ingest import check, extract
from ghrec.train import run

T = datetime(2026, 11, 8)


def test_extract_keeps_only_real_engagement(tmp_path):
    ev = [("WatchEvent", "started"), ("PushEvent", None), ("ForkEvent", "forked"), ("PullRequestEvent", "opened"),
          ("PullRequestEvent", "closed"), ("IssueCommentEvent", "created"), ("IssueCommentEvent", "deleted"),
          ("PullRequestReviewEvent", "created"), ("PullRequestReviewEvent", "dismissed"), ("ReleaseEvent", "published")]
    rows = [{"type": t, "actor": {"login": "a"}, "repo": {"name": "o/r"}, "created_at": "2026-11-01T01:02:03Z",
             "payload": {"action": a}} for t, a in ev]
    p = tmp_path / "h.json.gz"
    p.write_bytes(gzip.compress("\n".join(map(json.dumps, rows)).encode()))
    df = extract([str(p)])
    assert sorted(df["type"]) == sorted(["WatchEvent", "ForkEvent", "PullRequestEvent", "IssueCommentEvent", "PullRequestReviewEvent"])
    assert df["ts"][0].year == 2026


def test_clean_drops_bots_self_and_dupes():
    d = pl.DataFrame({"user": ["a", "a", "b[bot]", "o"], "repo": ["x/y", "x/y", "x/y", "o/z"],
                      "type": ["WatchEvent"] * 4, "ts": [T] * 4})
    assert clean(d, 1, 1).height == 1


def test_split_segments_are_time_ordered():
    s = split(synthetic(T), T)
    assert s["retr"]["ts"].max() < s["rank"]["ts"].min() <= s["rank"]["ts"].max() < s["test"]["ts"].min()


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    art = tmp_path_factory.mktemp("art")
    return art, run(synthetic(T), T, art)


def test_pipeline_beats_popularity_and_promotes(trained):
    m = trained[1]["challenger"]
    assert trained[1]["promoted"] and m["e2e_ndcg@10"] > m["popularity_ndcg@10"] > 0


def test_gate_blocks_equal_challenger(trained):
    assert not run(synthetic(T), T, trained[0])["promoted"]  # same data -> no strict win over champion


def test_api(trained, monkeypatch):
    monkeypatch.setenv("MODEL_PATH", str(trained[0] / "champion.pkl"))
    from service.app import app
    with TestClient(app) as c:
        body = {"likes": ["owner1/repo1", "owner2/repo2"], "session_id": "sess-12345"}
        r = c.post("/recommend", json=body)
        assert r.status_code == 200 and len(r.json()["items"]) == 10
        assert c.post("/recommend", json=body).json()["items"] == r.json()["items"]  # cached
        assert c.get("/health").json()["status"] == "ok"
        assert c.get("/trending").status_code == 200


CUT = T - timedelta(days=7)


def _rows(pairs, ts):
    return pl.DataFrame({"user": [u for u, _ in pairs], "repo": [r for _, r in pairs], "type": ["WatchEvent"] * len(pairs), "ts": [ts] * len(pairs)})


def test_kcore_ignores_test_period_activity():
    early = _rows([(f"u{i}", f"o/r{j}") for i in range(6) for j in range(3)], T - timedelta(days=30))
    # a brand-new repo and user that would pass k-core only thanks to test-period rows
    late = _rows([(f"u{i}", "o/new") for i in range(6)] + [("newbie", f"o/r{j}") for j in range(3)], T - timedelta(days=1))
    out = clean(pl.concat([early, late]), 3, 5, cutoff=CUT)
    assert "o/new" not in out["repo"].to_list() and "newbie" not in out["user"].to_list()
    assert out.filter(pl.col("ts") >= CUT).height == 0  # known users + known repos only; here no such late pair exists


def test_kcore_invariant_on_training_rows():
    out = clean(synthetic(T), 5, 10, cutoff=CUT)
    tr = out.filter(pl.col("ts") < CUT)
    assert tr.group_by("user").len()["len"].min() >= 5 and tr.group_by("repo").len()["len"].min() >= 10


def test_segments_disjoint_and_complete():
    df, _ = prepare(clean(synthetic(T), 5, 10, cutoff=CUT), T)
    seg = split(df, T)
    assert sum(p.height for p in seg.values()) == df.filter(pl.col("ts") >= T - timedelta(days=90)).height
    keys = pl.concat([p.select("user", "repo") for p in seg.values()])
    assert keys.height == keys.unique().height  # no pair in two segments


def test_snapshot_version_is_deterministic_and_order_free():
    df = clean(synthetic(T), 5, 10)
    assert snapshot_version(df, T) == snapshot_version(df.sample(fraction=1.0, shuffle=True, seed=1), T)
    assert snapshot_version(df, T) != snapshot_version(df.head(df.height - 1), T)


def test_features_use_only_training_segment():
    df, repos = prepare(clean(synthetic(T), 5, 10, cutoff=CUT), T)
    seg = split(df, T)
    p = Pipeline(repos)
    p.fit(seg["retr"], seg["rank"])
    assert np.allclose(p.pop, np.log1p(np.bincount(seg["retr"]["i"].to_numpy(), minlength=len(repos))))
    assert seg["retr"]["ts"].max() < T - timedelta(days=21)


def _fake_post(calls, fail_after=None):
    import re

    def post(q):
        calls.append(q)
        if fail_after is not None and len(calls) > fail_after:
            raise RateLimited("limit")
        data = {"rateLimit": {"remaining": 4000}}
        for j, o, n in re.findall(r'r(\d+): repository\(owner: "(.*?)", name: "(.*?)"\)', q):
            data[f"r{j}"] = None if o == "gone" else {
                "primaryLanguage": {"name": "Python"}, "repositoryTopics": {"nodes": [{"topic": {"name": "ml"}}]},
                "description": "d", "createdAt": "2020-01-01T00:00:00Z", "readme": {"text": "x" * 900}}
        return {"data": data}
    return post


def test_enrich_batches_caches_and_skips_bad_names(tmp_path):
    path, calls = tmp_path / "m.parquet", []
    repos = ["a/1", "a/2", "a/3", "gone/x", "bad name/y", "a/1"]
    assert enrich(repos, path, _fake_post(calls), batch=2)["fetched"] == 4 and len(calls) == 2
    m = pl.read_parquet(path)
    assert sorted(m["repo"]) == ["a/1", "a/2", "a/3", "gone/x"]  # invalid name never queried, duplicate deduped
    assert m.filter(pl.col("repo") == "gone/x")["created_at"][0] is None  # missing repo cached as null
    assert m.filter(pl.col("repo") == "a/1")["readme_head"][0] == "x" * 500
    calls.clear()
    assert enrich(repos, path, _fake_post(calls))["requested"] == 0 and not calls  # fresh cache: no API calls


def test_enrich_resumes_after_rate_limit_and_refetches_stale(tmp_path):
    path, calls = tmp_path / "m.parquet", []
    repos = [f"a/{i}" for i in range(6)]
    r = enrich(repos, path, _fake_post(calls, fail_after=1), batch=2)
    assert r == {"requested": 6, "fetched": 2, "remaining": 4}
    assert enrich(repos, path, _fake_post([]), batch=2)["fetched"] == 4  # resumes where it stopped
    later = datetime.utcnow() + timedelta(days=8)
    assert enrich(repos, path, _fake_post([]), now=later)["fetched"] == 6  # past the 7-day TTL
    assert pl.read_parquet(path).height == 12  # history is appended, not overwritten


def test_pipeline_uses_metadata_features():
    df, repos = prepare(clean(synthetic(T), 5, 10, cutoff=CUT), T)
    meta = pl.DataFrame({"repo": repos, "language": ["py" if i % 2 else "go" for i in range(len(repos))],
                         "topics": [["t%d" % (i % 3)] for i in range(len(repos))],
                         "created_at": [datetime(2020, 1, 1)] * len(repos), "fetched_at": [datetime(2026, 1, 1)] * len(repos)})
    seg = split(df, T)
    p = Pipeline(repos)
    p.set_meta(meta, T)
    p.fit(seg["retr"], seg["rank"])
    assert p.ranker.n_features_in_ == 2 * 2 + 1 + 4 and p.has_meta.sum() == len(repos)
    assert len(p.recommend(repos[:3], 10)) == 10


def test_metadata_is_as_of_the_cutoff():
    repos = ["a/1", "a/2"]
    meta = pl.DataFrame({"repo": ["a/1", "a/1", "a/2"], "language": ["Go", "Rust", "C"], "topics": [["old"], ["new"], ["x"]],
                         "created_at": [datetime(2020, 1, 1)] * 3,
                         "fetched_at": [datetime(2026, 1, 1), datetime(2026, 12, 1), datetime(2026, 12, 1)]})
    p = Pipeline(repos)
    p.set_meta(meta, T, asof=datetime(2026, 6, 1))
    assert p.topics[0] == {"old"}                      # the later fetch (after asof) is ignored
    assert p.lang[1] == -1 and p.topics[1] == frozenset()  # fetched after asof: language/topics unknown
    assert p.has_meta.tolist() == [1, 1] and p.age[1] > 6  # immutable created_at still gives age


def test_qa_compares_only_with_earlier_days(tmp_path):
    for d, n in [("2026-11-02", 50000), ("2026-11-03", 52000)]:
        (tmp_path / f"raw_events/date={d}").mkdir(parents=True)
        (tmp_path / f"raw_events/date={d}/manifest.json").write_text(json.dumps({"rows": n}))
    df = lambda n: pl.DataFrame({"user": ["a"] * n, "repo": ["o/r"] * n})
    assert check(df(21000), date(2026, 8, 12), tmp_path)["avg_7d"] is None  # earlier backfill day: nothing before it
    assert check(df(51000), date(2026, 11, 4), tmp_path)["rows"] == 51000
    with pytest.raises(ValueError):
        check(df(10000), date(2026, 11, 4), tmp_path)  # a real drop is still caught


class _FakeBucket:
    def __init__(self, store):
        self.store = store
        self.objects = self

    def all(self):
        return [type("O", (), {"key": k, "size": len(v)})() for k, v in self.store.items()]

    def download_file(self, key, path):
        open(path, "wb").write(self.store[key])

    def upload_file(self, path, key):
        self.store[key] = open(path, "rb").read()


def test_s3sync_round_trip_and_skips_unchanged(tmp_path, monkeypatch):
    store = {"raw_events/date=2026-11-01/events.parquet": b"abc"}
    root = tmp_path / "data"
    monkeypatch.setattr(s3sync, "_bucket", lambda: (_FakeBucket(store), root))
    s3sync.pull()
    assert (root / "raw_events/date=2026-11-01/events.parquet").read_bytes() == b"abc"
    (root / "raw_events/date=2026-11-02").mkdir()
    (root / "raw_events/date=2026-11-02/events.parquet").write_bytes(b"new!")
    s3sync.push()
    assert store["raw_events/date=2026-11-02/events.parquet"] == b"new!" and len(store) == 2


def _events(rows):
    return pl.DataFrame(rows, schema=["user", "repo", "type", "ts"], orient="row")


def test_features_are_point_in_time_and_windowed():
    asof = datetime(2026, 11, 10)
    ev = _events([
        ("a", "o/r", "WatchEvent", asof - timedelta(hours=1)),    # inside the 1d window
        ("b", "o/r", "WatchEvent", asof - timedelta(days=3)),     # 7d and 30d only
        ("c", "o/r", "ForkEvent", asof - timedelta(days=20)),     # event, not a star
        ("d", "o/r", "WatchEvent", asof - timedelta(days=40)),    # outside every window
        ("e", "o/r", "WatchEvent", asof),                          # AT asof: the future, must be ignored
        ("f", "o/r", "WatchEvent", asof + timedelta(days=2)),     # after asof: must be ignored
    ])
    r = repo_features(ev, asof).row(0, named=True)
    assert (r["stars_1d"], r["stars_7d"], r["stars_30d"], r["events_30d"], r["users_30d"]) == (1, 2, 2, 3, 3)
    only_past = ev.filter(pl.col("ts") < asof)
    assert repo_features(ev, asof).equals(repo_features(only_past, asof))  # future rows change nothing
    assert user_features(ev, asof).filter(pl.col("user") == "e").height == 0  # a user who only appears at/after asof


def test_user_features_count_distinct_repos():
    asof = datetime(2026, 11, 10)
    ev = _events([("a", "o/1", "WatchEvent", asof - timedelta(days=1)), ("a", "o/1", "ForkEvent", asof - timedelta(days=2)),
                  ("a", "o/2", "IssuesEvent", asof - timedelta(days=9))])
    u = user_features(ev, asof).row(0, named=True)
    assert (u["events_7d"], u["events_30d"], u["repos_30d"], u["stars_30d"]) == (2, 3, 2, 1)
