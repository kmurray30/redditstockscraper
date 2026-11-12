import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import discover as dv  # noqa: E402

DAY = 86400
CUT = 1_425_168_000   # 2015-03-01, the cohort cutoff


def write(tmp, posts, comments=()):
    (tmp / "corpus" / "posts").mkdir(parents=True)
    (tmp / "corpus" / "comments").mkdir(parents=True)
    (tmp / "features").mkdir()
    pq.write_table(pa.Table.from_pylist([
        {"id": p["id"], "subreddit": p.get("sub", "stocks"), "created_utc": p["t"], "author": p.get("a", "x"),
         "title": p.get("title", "t"), "selftext": p.get("text", ""), "flair": None, "url": None, "domain": None,
         "is_self": True, "edited_utc": p.get("edited", 0), "score": 1, "num_comments": 0} for p in posts]),
        tmp / "corpus" / "posts" / "stocks.parquet")
    pq.write_table(pa.Table.from_pylist(
        [{"id": f"c{i}", "post_id": pid, "parent_id": f"t3_{pid}", "created_utc": t, "author": "y", "body": "b",
          "score": 1, "edited_utc": 0} for i, (pid, t) in enumerate(comments)]
        or [{"id": "c", "post_id": "none", "parent_id": "", "created_utc": 0, "author": "y", "body": "", "score": 0,
             "edited_utc": 0}]), tmp / "corpus" / "comments" / "stocks.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"source": "post", "source_id": p["id"], "post_id": p["id"], "cik": cik, "ticker": "T" + cik,
         "method": "cashtag", "n": 1, "in_title": True} for p in posts for cik in p.get("co", ["1"])]),
        tmp / "features" / "mentions.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"post_id": p["id"], "f_bull": 0, "f_bear": 0, "f_sent": 0.0, "f_primary_links": 0, "f_news_links": 0,
         "f_links": 0, "f_chars": p.get("chars", 1000), "f_dd": False} for p in posts]), tmp / "features" / "pf.parquet")
    pq.write_table(pa.Table.from_pylist([
        {"month": "2015-03-01", "cik": c, "ticker": "T" + c, "code": "T" + c, "sec_type": "CS", "price": 10.0,
         "adv60": 1e6, "mom": 0.1 * int(c), "eligible": True, "size_q": 1} for c in ("1", "2", "3")]),
        tmp / "features" / "universe.parquet")
    dv.build_post_class(tmp / "corpus", tmp / "features" / "pc.parquet", log=lambda *_: None)


def panel(tmp):
    con = duckdb.connect()
    f = tmp / "features"
    dv.build_panel(con, tmp / "corpus", f / "mentions.parquet", f / "pf.parquet", f / "pc.parquet", f,
                   "2015-03-01", "2015-03-01", log=lambda *_: None)
    return con, {r[0]: dict(zip([d[0] for d in con.description], r))
                 for r in [con.execute("select cik, * exclude (cik) from panel order by cik").fetchall()][0]}


def test_post_class_counts_vocabularies_and_questions():
    c = dv.post_class("Thoughts on $ABC?", "Revenue up, free cash flow positive, debt falling. Not a squeeze play.")
    assert c["fund_n"] == 3 and c["spec_n"] == 1 and c["is_question"]
    assert not dv.post_class("ABC: the bull case", "chart breakout, to the moon \U0001F680")["is_question"]
    assert dv.post_class("x", "chart breakout \U0001F680")["spec_n"] == 3


def test_panel_sees_only_matured_unedited_posts_and_first_day_comments(tmp_path):
    posts = [
        {"id": "a", "t": CUT - 10 * DAY, "a": "u1"},                       # visible, substantive
        {"id": "b", "t": CUT - 20 * DAY, "a": "u2", "chars": 100},         # visible, thin
        {"id": "late", "t": CUT - 23 * 3600, "a": "u3"},                   # not matured by the cutoff
        {"id": "ed", "t": CUT - 30 * DAY, "a": "u4", "edited": CUT - 25 * DAY},  # edited after 24h
        {"id": "old", "t": CUT - 200 * DAY, "a": "u5", "co": ["2"]},       # prior year for company 2
        {"id": "new2", "t": CUT - 5 * DAY, "a": "u6", "co": ["2"]},
        {"id": "n3", "t": CUT - 5 * DAY, "a": "u7", "co": ["3"]},          # novel for company 3
    ]
    comments = [("a", CUT - 10 * DAY + 3600), ("a", CUT - 10 * DAY + 2 * DAY), ("a", CUT - 9 * DAY - 3600)]
    write(tmp_path, posts, comments)
    _, p = panel(tmp_path)
    assert p["1"]["n_posts"] == 2 and p["1"]["n_subst"] == 1 and p["1"]["c24"] == 2
    assert p["2"]["n_subst"] == 1 and p["2"]["prior_subst"] == 1
    assert p["3"]["n_subst"] == 1 and p["3"]["prior_subst"] == 0


def test_cluster_gap_and_bh():
    rng = np.random.default_rng(1)
    d = np.r_[rng.normal(0.2, 0.1, 300), rng.normal(0.2, 0.1, 300)]
    g = np.r_[np.arange(300), np.arange(300)]
    r = dv.cluster_gap(d, g, 300)
    assert r["lo"] < 0.2 < r["hi"] and r["z"] > 10 and r["companies"] == 300
    assert dv.bh({"a": 0.001, "b": 0.04, "c": 0.5}, 0.10) == {"a", "b"}
    assert dv.cluster_gap(np.array([]), np.array([]))["n"] == 0


def test_verdicts_follow_the_registered_rule():
    def g(gap, lo, hi, z):
        return {"n": 100, "gap": gap, "lo": lo, "hi": hi, "z": z}

    def cls(d, c1, c2):
        return {"D": {3: {"beat": d}}, "C1": {3: {"beat": c1}}, "C2": {1: {"beat": c2}}}

    res = {"good": cls(g(.05, .02, .08, 3), g(.03, 0, .06, 2.5), g(.02, 0, .04, 2.2)),
           "flip": cls(g(.05, .02, .08, 3), g(-.01, -.03, .01, -1), g(.02, 0, .04, 2)),
           "weak": cls(g(.01, -.01, .03, 1), g(.01, -.02, .04, .5), g(.01, -.02, .03, .4)),
           "avoid": cls(g(-.06, -.09, -.03, -4), g(-.04, -.07, -.01, -3), g(-.03, -.05, -.01, -3))}
    v = dv.verdicts(res, "beat")
    assert v == {"good": "SURVIVES", "flip": "FAILS", "weak": "CONSISTENT", "avoid": "SURVIVES"}


def test_main_runs_end_to_end_on_a_tiny_corpus(tmp_path, capsys):
    write(tmp_path, [{"id": "a", "t": CUT - 10 * DAY}])
    f = tmp_path / "features"
    pq.write_table(pa.Table.from_pylist([
        {"month": "2015-03-01", "cik": c, "years": y, "status": "complete", "ret": 0.1, "cagr": 0.1,
         "excess": e, "max_mult": 1.5, "min_mult": 0.9, "max_dd": 0.1, "d2": None, "d5": None, "d10": None,
         "perm_loss": False, "entry_day": 0, "end_day": 1}
        for c, e in (("1", 0.3), ("2", -0.1), ("3", 0.05)) for y in (1, 3, 5)]), f / "labels.parquet")
    out = tmp_path / "res.json"
    assert dv.main(["--parquet", str(tmp_path / "corpus"), "--mentions", str(f / "mentions.parquet"),
                    "--post-features", str(f / "pf.parquet"), "--post-class", str(f / "pc.parquet"),
                    "--features", str(f), "--first", "2015-03-01", "--last", "2015-03-01", "--boot", "50",
                    "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["classes"]["substantive"]["D"]["3"]["beat"]["n"] == 1
    assert set(res["verdicts"]["beat"].values()) == {"NO DATA"}
    assert len(res["classes"]) == len(dv.CLASSES)


def test_robustness_round_runs_and_drops_the_top_contributors(tmp_path):
    cik = np.array(["a"] * 3 + ["b"] * 3 + ["c"])
    d = np.array([1.0, 1, 1, .1, .1, .1, -.5])
    c2, d2 = dv.drop_top(cik, d, 1, k=1)
    assert set(c2) == {"b", "c"} and len(d2) == 4
    w = dv.company_weighted(cik, d, 50)
    assert abs(w["gap"] - (1 + .1 - .5) / 3) < 1e-9
    write(tmp_path, [{"id": "a", "t": CUT - 10 * DAY}])
    f = tmp_path / "features"
    pq.write_table(pa.Table.from_pylist([
        {"month": "2015-03-01", "cik": c, "years": y, "status": "complete", "ret": 0.1, "cagr": 0.1,
         "excess": e, "max_mult": 1.5, "min_mult": 0.9, "max_dd": 0.1, "d2": None, "d5": None, "d10": None,
         "perm_loss": False, "entry_day": 0, "end_day": 1}
        for c, e in (("1", 0.3), ("2", -0.1), ("3", 0.05)) for y in (1, 3, 5)]), f / "labels.parquet")
    out = tmp_path / "res.json"
    assert dv.main(["--parquet", str(tmp_path / "corpus"), "--mentions", str(f / "mentions.parquet"),
                    "--post-features", str(f / "pf.parquet"), "--post-class", str(f / "pc.parquet"),
                    "--features", str(f), "--first", "2015-03-01", "--last", "2015-03-01", "--boot", "20",
                    "--sic", str(tmp_path / "missing.parquet"), "--out", str(out), "--robust"]) == 0
    rob = json.loads((tmp_path / "res_robust.json").read_text())
    assert set(rob) == {n for n, _ in dv.ROBUST} and rob["broad"]["verdict"] == "FRAGILE"


def test_round3_joins_health_and_market_cap(tmp_path):
    write(tmp_path, [{"id": "a", "t": CUT - 10 * DAY}])
    f = tmp_path / "features"
    pq.write_table(pa.Table.from_pylist([
        {"month": "2015-03-01", "cik": c, "years": y, "status": "complete", "ret": 0.1, "cagr": 0.1,
         "excess": e, "max_mult": 1.5, "min_mult": 0.9, "max_dd": 0.1, "d2": None, "d5": None, "d10": None,
         "perm_loss": False, "entry_day": 0, "end_day": 1}
        for c, e in (("1", 0.3), ("2", -0.1), ("3", 0.05)) for y in (1, 3, 5)]), f / "labels.parquet")
    pq.write_table(pa.Table.from_pylist([{"month": "2015-03-01", "cik": "1", "health": "pass", "mcap": 5e8},
                                         {"month": "2015-03-01", "cik": "2", "health": "fail", "mcap": 1e8}]),
                   f / "fund.parquet")
    con = duckdb.connect()
    dv.build_panel(con, tmp_path / "corpus", f / "mentions.parquet", f / "pf.parquet", f / "pc.parquet", f,
                   "2015-03-01", "2015-03-01", log=lambda *_: None, fund_path=f / "fund.parquet")
    rows = dict(con.execute("select cik, health || ':' || mcap_q from panel").fetchall())
    assert rows == {"1": "pass:2", "2": "fail:1", "3": "unknown:0"}
    out = tmp_path / "res.json"
    assert dv.main(["--parquet", str(tmp_path / "corpus"), "--mentions", str(f / "mentions.parquet"),
                    "--post-features", str(f / "pf.parquet"), "--post-class", str(f / "pc.parquet"),
                    "--features", str(f), "--first", "2015-03-01", "--last", "2015-03-01", "--boot", "20",
                    "--fundamentals", str(f / "fund.parquet"), "--out", str(out), "--round3"]) == 0
    r3 = json.loads((tmp_path / "res_round3.json").read_text())
    assert set(r3["classes"]) == {n for n, *_ in dv.ROUND3}
    assert r3["classes"]["subst_pass"]["D"]["3"]["beat"]["n"] == 1
