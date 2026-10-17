import sys
from pathlib import Path

import pyarrow as pa
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import baselines as bl  # noqa: E402
import pit  # noqa: E402
import scorer as sc  # noqa: E402


def test_sentiment_counts_with_negation():
    assert bl.sentiment_counts("I am bullish, buy it") == (2, 0)
    assert bl.sentiment_counts("this is not bullish, a scam") == (0, 2)
    assert bl.sentiment_counts("not a scam") == (1, 0)


def test_link_counts_classifies_and_survives_garbage():
    text = ("see https://www.sec.gov/Archives/edgar/x and https://www.wsj.com/a and "
            "https://example.com/b and http://[broken")
    assert bl.link_counts(text) == (1, 1, 4)
    assert bl.link_counts("", "https://investor.apple.com/x") == (1, 0, 1)
    assert bl.link_counts("", "https://www.reddit.com/r/x") == (0, 0, 0)


def test_percentile_ranks_ties_share_a_rank():
    r = bl.percentile_ranks({"a": 1, "b": 2, "c": 2, "d": 9})
    assert r["a"] == 0 and r["d"] == 1 and r["b"] == r["c"] == pytest.approx(0.5)


def test_post_features_flags_dd_and_long_posts():
    f = bl.post_features({"id": "p", "title": "ABC DD", "selftext": "x" * 2000, "flair": "DD",
                          "url": None})
    assert f["f_dd"] and f["f_chars"] == 2000


def post_row(pid, cik, **kw):
    base = {"cik": cik, "ticker": cik, "sec_type": "CS", "method": "cashtag",
            "resolve_conf": "stable", "n": 1, "in_title": True, "post_id": pid,
            "subreddit": "s", "created_utc": 1, "author": "a" + pid, "title": "t",
            "selftext": "x", "flair": None, "url": "u", "domain": "d", "is_self": True,
            "edited_utc": 0, "n_companies": 1, "f_bull": 0, "f_bear": 0, "f_sent": 0.0, "f_primary_links": 0,
            "f_news_links": 0, "f_links": 0, "f_chars": 100, "f_dd": False}
    return {**base, **kw}


def cohort(rows, comment_mentions=None, thread=None, universe=None):
    pm = pa.Table.from_pylist(rows)
    cm = pa.Table.from_pylist(comment_mentions or [], schema=pa.schema([
        ("cik", pa.string()), ("ticker", pa.string()), ("method", pa.string()), ("n", pa.int32()),
        ("comment_id", pa.string()), ("post_id", pa.string()), ("created_utc", pa.int64()),
        ("author", pa.string())]))
    tc = pa.Table.from_pylist(thread or [], schema=pa.schema([
        ("comment_id", pa.string()), ("post_id", pa.string()), ("parent_id", pa.string()),
        ("created_utc", pa.int64()), ("author", pa.string()), ("body", pa.string()),
        ("edited_utc", pa.int64())]))
    view = pit.CohortView(100, 90, pm, cm, tc)
    uni = universe or {r["cik"]: {"ticker": r["cik"], "mom": 0.1} for r in rows}
    return sc.Cohort("2020-01-01", 100, view, uni)


def scores(baseline, c):
    return {k: v["score"] for k, v in baseline.score(c).items()}


def test_mentions_counts_distinct_posts_and_respects_universe():
    rows = [post_row("p1", "A"), post_row("p2", "A"), post_row("p3", "B"),
            post_row("p4", "NOTELIGIBLE")]
    c = cohort(rows, universe={"A": {"mom": 0.0}, "B": {"mom": 0.0}})
    assert scores(bl.Baseline("mentions"), c) == {"A": 2.0, "B": 1.0}


def test_name_matches_are_excluded_by_default():
    rows = [post_row("p1", "A", method="name"), post_row("p2", "B")]
    assert set(scores(bl.Baseline("mentions"), cohort(rows))) == {"B"}
    assert set(scores(bl.Baseline("mentions", methods=bl.ALL_METHODS), cohort(rows))) == {"A", "B"}


def test_mentions_all_adds_comment_mentions():
    rows = [post_row("p1", "A")]
    cm = [{"cik": "A", "ticker": "A", "method": "cashtag", "n": 1, "comment_id": "c1",
           "post_id": "p1", "created_utc": 2, "author": "z"}]
    assert scores(bl.Baseline("mentions_all"), cohort(rows, cm)) == {"A": 2.0}


def test_sentiment_and_dd_and_evidence():
    rows = [post_row("p1", "A", f_sent=0.5, f_dd=True, f_primary_links=2),
            post_row("p2", "B", f_sent=-0.5, f_news_links=1)]
    c = cohort(rows)
    assert scores(bl.Baseline("sentiment"), c) == {"A": 0.5, "B": -0.5}
    assert scores(bl.Baseline("dd"), c) == {"A": 1.0, "B": 0.0}
    assert scores(bl.Baseline("evidence"), c) == {"A": 4.0, "B": 1.0}


def test_crowd_penalty_demotes_the_most_mentioned_name():
    rows = [post_row(f"p{i}", "CROWDED", f_dd=True) for i in range(5)] + \
           [post_row("q1", "QUIET", f_dd=True)]
    s = scores(bl.Baseline("crowd_penalized"), cohort(rows))
    assert s["QUIET"] > s["CROWDED"]


def test_momentum_uses_the_universe_not_reddit():
    c = cohort([post_row("p1", "A")], universe={"A": {"mom": 0.4}, "Z": {"mom": -0.1},
                                                "N": {"mom": None}})
    assert scores(bl.Baseline("momentum"), c) == {"A": 0.4, "Z": -0.1}


def test_random_is_deterministic_per_seed_and_varies_by_month():
    c = cohort([post_row(f"p{i}", f"C{i}") for i in range(20)])
    a, b = scores(bl.Baseline("random", seed=1), c), scores(bl.Baseline("random", seed=1), c)
    other = scores(bl.Baseline("random", seed=2), c)
    assert a == b and a != other and all(0 <= v < 1 for v in a.values())


def test_min_posts_filter():
    rows = [post_row("p1", "A"), post_row("p2", "A"), post_row("p3", "B")]
    assert set(scores(bl.Baseline("mentions", min_posts=2), cohort(rows))) == {"A"}


def test_config_hash_changes_with_params_only():
    a, b = bl.Baseline("mentions"), bl.Baseline("mentions", min_posts=2)
    assert sc.config_hash(a) == sc.config_hash(bl.Baseline("mentions"))
    assert sc.config_hash(a) != sc.config_hash(b)
    assert isinstance(a, sc.Scorer)


def test_list_posts_naming_many_companies_are_ignored():
    rows = [post_row("list", "A", n_companies=40), post_row("list", "B", n_companies=40),
            post_row("real", "A", n_companies=1)]
    assert scores(bl.Baseline("mentions"), cohort(rows)) == {"A": 1.0}
    assert scores(bl.Baseline("mentions", max_companies=100), cohort(rows)) == {"A": 2.0, "B": 1.0}
