import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import pit  # noqa: E402

H = 3600
T0 = 1_600_000_000  # an arbitrary "now"


def post(pid, created, **kw):
    base = {"id": pid, "subreddit": "s", "created_utc": created, "author": "a", "title": "t",
            "selftext": "x", "score": 999, "upvote_ratio": 0.9, "num_comments": 50, "flair": None,
            "url": "u", "domain": "d", "is_self": True, "matched_tickers": [], "edited_utc": 0}
    return {**base, **kw}


def comment(cid, pid, created, **kw):
    base = {"id": cid, "post_id": pid, "parent_id": "t3_" + pid, "created_utc": created,
            "author": "c", "body": "b", "score": 77, "edited_utc": 0}
    return {**base, **kw}


def mention(source, sid, pid, created, cik="1"):
    return {"source": source, "source_id": sid, "post_id": pid, "subreddit": "s",
            "created_utc": created, "cik": cik, "ticker": "AAA", "sec_type": "CS",
            "method": "cashtag", "resolve_conf": "stable", "n": 1, "in_title": True}


@pytest.fixture
def corpus(tmp_path):
    posts = [
        post("old", T0 - 40 * 86400),                       # outside a 30d window
        post("ok", T0 - 5 * 86400),                          # matured, in window
        post("fresh", T0 - 10 * H),                          # created 10h ago: not matured (24h)
        post("edited_late", T0 - 5 * 86400, edited_utc=T0 - 86400),    # edited after maturation
        post("edited_early", T0 - 5 * 86400, edited_utc=T0 - 5 * 86400 + H),
        post("edited_unknown", T0 - 5 * 86400, edited_utc=-1),
    ]
    comments = [
        comment("c_in", "ok", T0 - 5 * 86400 + 2 * H),       # inside the post's 24h window
        comment("c_late", "ok", T0 - 5 * 86400 + 30 * H),    # after the window: invisible
        comment("c_edited_late", "ok", T0 - 5 * 86400 + H, edited_utc=T0 - 86400),
    ]
    mentions = [mention("post", p["id"], p["id"], p["created_utc"]) for p in posts]
    mentions += [mention("comment", "c_in", "ok", T0 - 5 * 86400 + 2 * H),
                 mention("comment", "c_late", "ok", T0 - 5 * 86400 + 30 * H)]
    (tmp_path / "posts").mkdir()
    (tmp_path / "comments").mkdir()
    pq.write_table(pa.Table.from_pylist(posts), tmp_path / "posts" / "s.parquet")
    pq.write_table(pa.Table.from_pylist(comments), tmp_path / "comments" / "s.parquet")
    pq.write_table(pa.Table.from_pylist(mentions), tmp_path / "mentions.parquet")
    return pit.PITCorpus(tmp_path, tmp_path / "mentions.parquet")


def ids(view):
    return {r["post_id"] for r in view.posts()}


def test_only_matured_posts_in_the_window_are_visible(corpus):
    v = corpus.at(T0, 30)
    assert ids(v) == {"ok", "edited_early"}   # not old/fresh, nor posts edited after maturation


def test_a_post_becomes_visible_exactly_when_it_matures(corpus):
    just_before = corpus.at(T0 - 10 * H + 24 * H - 1, 30)
    just_after = corpus.at(T0 - 10 * H + 24 * H, 30)
    assert "fresh" not in ids(just_before) and "fresh" in ids(just_after)


def test_moving_the_cutoff_back_never_reveals_more(corpus):
    later, earlier = ids(corpus.at(T0, 30)), ids(corpus.at(T0 - 3 * 86400, 30))
    assert earlier <= later and "fresh" not in earlier


def test_comments_after_the_maturation_window_are_invisible(corpus):
    v = corpus.at(T0, 30)
    got = {r["comment_id"] for r in v.comments_for(["ok"])}
    assert got == {"c_in"}                                   # c_late too late, c_edited_late edited
    assert {r["comment_id"] for r in v.query("select * from comment_mentions")} == {"c_in"}


def test_edited_unknown_time_is_treated_as_edited_after(corpus):
    assert "edited_unknown" not in ids(corpus.at(T0, 30))


def test_keep_policy_lets_edited_posts_through(corpus):
    keep = pit.PITCorpus(corpus.root, corpus.mentions_path, pit.PITConfig(edited_policy="keep"))
    assert {"edited_late", "edited_unknown"} <= ids(keep.at(T0, 30))


def test_no_final_engagement_numbers_exist_in_the_view(corpus):
    v = corpus.at(T0, 30)
    for table in ("post_mentions", "comment_mentions", "thread_comments"):
        cols = {d[0] for d in v._con.execute(f"select * from {table} limit 0").description}
        assert cols and not cols & {"score", "num_comments", "upvote_ratio"}, table


def test_the_view_database_cannot_reach_the_full_corpus(corpus):
    v = corpus.at(T0, 30)
    with pytest.raises(Exception):
        v.query("select * from posts")
    with pytest.raises(Exception):
        v.query("select * from mentions")


def test_bad_policy_rejected():
    with pytest.raises(ValueError):
        pit.PITConfig(edited_policy="nope")


def test_each_visible_post_reports_how_many_companies_it_names(tmp_path):
    posts = [post("list", T0 - 5 * 86400), post("single", T0 - 5 * 86400)]
    mentions = [mention("post", "list", "list", T0 - 5 * 86400, cik=str(i)) for i in range(12)]
    mentions += [mention("post", "single", "single", T0 - 5 * 86400, cik="1")]
    (tmp_path / "posts").mkdir()
    (tmp_path / "comments").mkdir()
    pq.write_table(pa.Table.from_pylist(posts), tmp_path / "posts" / "s.parquet")
    pq.write_table(pa.Table.from_pylist([comment("c", "list", T0)]), tmp_path / "comments" / "s.parquet")
    pq.write_table(pa.Table.from_pylist(mentions), tmp_path / "mentions.parquet")
    v = pit.PITCorpus(tmp_path, tmp_path / "mentions.parquet").at(T0, 30)
    got = {r["post_id"]: r["n_companies"] for r in v.posts()}
    assert got == {"list": 12, "single": 1}
