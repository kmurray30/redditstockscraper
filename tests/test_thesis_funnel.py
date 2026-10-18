import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import pit  # noqa: E402
import thesis_funnel as tf  # noqa: E402

DAY = 86400
M1 = "2015-02-01"           # cutoff months
M2 = "2015-03-01"
from backtest import month_utc  # noqa: E402

T1, T2 = month_utc(M1), month_utc(M2)


def post(pid, created, body_chars=1000, author="a", is_self=True, score=10, flair=None, url="u"):
    return {"id": pid, "subreddit": "stocks", "created_utc": created, "author": author,
            "title": f"title {pid}", "selftext": "x" * body_chars, "score": score,
            "upvote_ratio": 0.9, "num_comments": 1, "flair": flair, "url": url, "domain": "d",
            "is_self": is_self, "matched_tickers": [], "edited_utc": 0}


def mention(pid, created, cik="1", method="cashtag"):
    return {"source": "post", "source_id": pid, "post_id": pid, "subreddit": "stocks",
            "created_utc": created, "cik": cik, "ticker": "T" + cik, "sec_type": "CS",
            "method": method, "resolve_conf": "stable", "n": 1, "in_title": True}


def feat(p, primary=0):
    return {"post_id": p["id"], "f_bull": 0, "f_bear": 0, "f_sent": 0.0, "f_primary_links": primary,
            "f_news_links": 0, "f_links": primary, "f_chars": len(p["selftext"]),
            "f_dd": (p["flair"] or "").lower() == "dd"}


def build(tmp_path, posts, mentions, feats, comments=()):
    for d in ("posts", "comments"):
        (tmp_path / d).mkdir(exist_ok=True)
    pq.write_table(pa.Table.from_pylist(posts), tmp_path / "posts" / "s.parquet")
    pq.write_table(pa.Table.from_pylist(list(comments) or [
        {"id": "c0", "post_id": "none", "parent_id": "t3_none", "created_utc": 0, "author": "z",
         "body": "b", "score": 1, "edited_utc": 0}]), tmp_path / "comments" / "s.parquet")
    pq.write_table(pa.Table.from_pylist(mentions), tmp_path / "mentions.parquet")
    pq.write_table(pa.Table.from_pylist(feats), tmp_path / "pf.parquet")
    return pit.PITCorpus(tmp_path, tmp_path / "mentions.parquet", pit.PITConfig(votes="final"),
                         post_features=tmp_path / "pf.parquet")


def test_reject_reasons():
    base = {"method": "bare_clean", "n_companies": 1, "author": "a", "is_self": True, "f_chars": 1000,
            "in_title": True, "n": 1}
    assert tf.reject_reason(base) is None
    # a passing mention is not a thread about the company
    assert tf.reject_reason({**base, "in_title": False}) == "passing_mention"
    assert tf.reject_reason({**base, "in_title": False, "n": 2}) is None
    assert tf.reject_reason({**base, "in_title": False, "method": "cashtag"}) is None
    assert tf.reject_reason({**base, "method": "name"}) == "unreliable_mention"
    assert tf.reject_reason({**base, "n_companies": 3}) == "many_companies"
    assert tf.reject_reason({**base, "author": "[deleted]"}) == "deleted_author"
    assert tf.reject_reason({**base, "f_chars": 100}) == "thin_self_post"
    assert tf.reject_reason({**base, "is_self": False, "f_chars": 10}) == "bare_link"
    assert tf.reject_reason({**base, "is_self": False, "f_chars": 10, "f_primary_links": 1}) is None
    assert tf.reject_reason({**base, "is_self": False, "f_chars": 300}) is None
    # focus rule: a long DD naming 4 companies is kept when this one is in the title or dominant
    many = {**base, "n_companies": 4, "n": 2, "in_title": False}
    assert tf.reject_reason(many, post_max_n=3) == "many_companies"
    assert tf.reject_reason({**many, "in_title": True}, post_max_n=3) is None
    assert tf.reject_reason({**many, "n": 3}, post_max_n=3) is None
    assert tf.reject_reason({**many, "n": 2}, post_max_n=2) is None        # the most-mentioned company
    assert tf.reject_reason({**many, "n": 1}, post_max_n=1) == "passing_mention"   # once each: a list
    assert tf.reject_reason({**many, "in_title": True, "n_companies": 9}, post_max_n=3) == "many_companies"
    # discussion rule: a thin post with a real comment thread and votes is kept
    thin = {**base, "f_chars": 100, "votes": 50}
    assert tf.reject_reason(thin, n_comments=5) == "thin_self_post"
    assert tf.reject_reason(thin, n_comments=20) is None
    assert tf.reject_reason({**thin, "votes": 2}, n_comments=20) == "thin_self_post"


def test_selection_is_point_in_time_and_capped(tmp_path):
    t = T1 - 20 * DAY
    posts = [post("big", t, 5000, score=300), post("mid", t + DAY, 2000, score=50, flair="DD"),
             post("small", t + 2 * DAY, 500, score=1), post("tiny", t + 3 * DAY, 500, score=0),
             post("thin", t, 100), post("other", t, 3000, score=20),
             post("late", T1 + 10 * DAY, 4000, score=999),         # only visible at M2
             post("deleted", t, 3000, author="[deleted]"),
             post("talk", t, 50, score=40)]                         # thin, but 20 comments
    mentions = [mention(p["id"], p["created_utc"]) for p in posts if p["id"] != "other"]
    mentions.append(mention("other", t, cik="2"))
    comments = [{"id": f"c{i}", "post_id": "talk", "parent_id": "t3_talk", "created_utc": t + 60 * i,
                 "author": "z", "body": "a substantive comment", "score": 1, "edited_utc": 0}
                for i in range(20)]
    corpus = build(tmp_path, posts, mentions, [feat(p) for p in posts], comments)
    uni = {M1: {"1": {}, "2": {}}, M2: {"1": {}}}           # company 2 drops out of the universe at M2
    threads, census = tf.plan(corpus, [M1, M2], uni, lookback=90, top_k=3, log=lambda *_: None)
    by = {t["post_id"]: t for t in threads}
    # M1: company 1 has big, mid, small, tiny, talk (thin and deleted rejected) -> top 3 by
    # pre-score: big (votes, length), mid (DD flair), talk (votes, comments); small and tiny out
    assert {"big", "mid", "talk"} <= set(by) and "small" not in by and "tiny" not in by
    assert "thin" not in by and "deleted" not in by
    assert by["talk"]["n_comments"] == 20                               # the discussion rule
    assert "other" in by and by["other"]["first_month"] == M1           # in universe at M1
    # M2: "late" is visible and selected; "big" is selected again (two cutoffs, scored once)
    assert "late" in by and by["late"]["first_month"] == M2
    assert by["big"]["n_months"] == 2 and by["late"]["n_months"] == 1
    assert census["rejects"]["thin_self_post"] == 2 and census["rejects"]["deleted_author"] == 2
    assert census["rejects"]["not_in_universe"] == 1                     # "other" at M2
    assert census["rejects"]["over_cap"] >= 2                            # small and tiny at M1
    assert census["n_threads"] == len(threads) == 5
    assert all(t["matured_utc"] == t["created_utc"] + 24 * 3600 for t in threads)
    assert by["big"]["votes"] == 300 and by["big"]["est_tokens"] > by["talk"]["est_tokens"]


def test_dedupe_prefers_the_most_reliable_method():
    rows = [{"post_id": "p", "cik": "1", "method": "bare_context"},
            {"post_id": "p", "cik": "1", "method": "cashtag"},
            {"post_id": "p", "cik": "2", "method": "name"}]
    out = {(r["post_id"], r["cik"]): r["method"] for r in tf.dedupe(rows)}
    assert out == {("p", "1"): "cashtag", ("p", "2"): "name"}


def test_census_text_and_cost():
    threads = [{"ticker": "A", "subreddit": "s", "first_month": "2015-01-01", "votes": 3, "chars": 10,
                "n_comments": 0, "pre_score": 0.5, "est_tokens": 1000}] * 3
    census = {"stage": {"2015": {"cohorts": 2, "rows_seen": 10, "selected": 4, "unique_threads": 3,
                                 "tokens": 3000}},
              "rejects": {"thin_self_post": 6}, "n_threads": 3, "tokens": 3000}
    c = tf.cost(census, price_in=1.0, price_out=2.0)
    assert abs(c["identified"] - (3000 * 1.0 + 3 * tf.OUT_TOKENS * 2.0) / 1e6) < 1e-12
    text = tf.format_census(threads, census, 1.0, 2.0, "m")
    assert "TOTAL unique threads 3" in text and "thin_self_post" in text and "both $" in text
