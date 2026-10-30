import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import darlings as dl  # noqa: E402

LV = dl.Level("t", argument=50, evidence=50, support=50, voices=2, comments=10)


def g(post_id, cik, month, **kw):
    base = {"post_id": post_id, "cik": cik, "ticker": "T" + cik, "name": "Co " + cik, "first_month": month,
            "author": "a" + post_id, "stance": 0.8, "direction_5": 5, "argument_quality": 75, "evidence_quality": 75,
            "independent_support": 75, "n_independent_voices": 3, "n_comments": 20, "independent_opposition": 0,
            "strongest_objection_severity": 25, "late_shown": 0, "edited_late": False}
    base.update(kw)
    return base


def test_thread_rule_and_reasons():
    assert dl.failures(g("p", "1", "2015-01-01"), LV, True) == []
    assert "not bullish" in dl.failures(g("p", "1", "2015-01-01", stance=-0.5), LV, True)[0]
    assert dl.failures(g("p", "1", "2015-01-01", direction_5=2), LV, True)      # comments turned it bearish
    assert dl.failures(g("p", "1", "2015-01-01", direction_5=None), LV, True) == []   # a v1 grade: stance only
    assert dl.failures(g("p", "1", "2015-01-01", n_comments=3), LV, True) == ["comments 3 < 10"]
    heavy = g("p", "1", "2015-01-01", independent_opposition=75)
    assert dl.failures(heavy, LV, True) == ["opposition 75 >= 50"] and dl.failures(heavy, LV, False) == []


def test_events_cool_down_per_company_and_count_the_case():
    gs = [g("a", "1", "2015-01-01"), g("b", "1", "2015-02-01"), g("c", "1", "2015-12-01"),
          g("d", "1", "2016-01-01"), g("e", "2", "2015-06-01", argument_quality=10), g("f", "3", "2019-01-01")]
    ev = dl.events(gs, LV, True, (2012, 2018))
    assert [(e["cik"], e["month"]) for e in ev] == [("1", "2015-01-01"), ("1", "2016-01-01")]
    assert ev[0]["n_threads"] == 1 and ev[1]["threads"] == ["c", "d"] and ev[1]["authors"] == 2
    assert dl.per_year(ev, (2014, 2016)) == {2014: 0, 2015: 1, 2016: 1}


def test_pick_level_is_the_strictest_meeting_the_floor(monkeypatch):
    monkeypatch.setattr(dl, "MIN_PER_YEAR", 2)
    loose_only = [g(f"x{i}", str(i), f"{y}-0{1 + i % 5}-01", argument_quality=55, evidence_quality=30,
                    independent_support=30, n_comments=6) for i, y in enumerate([2012, 2012, 2013, 2013])]
    strong = [g(f"s{i}", str(100 + i), f"{y}-03-01") for i, y in enumerate([2012, 2012, 2013, 2013])]
    lv, table = dl.pick_level(loose_only + strong, (2012, 2013))
    assert table["L1 loose"]["strict"] == {2012: 4, 2013: 4} and lv.name != "L1 loose"
    assert min(table[lv.name]["strict"].values()) >= 2
    lv2, _ = dl.pick_level(loose_only[:1], (2012, 2013))          # nobody reaches the floor: loosest
    assert lv2 is dl.LEVELS[0]


def test_outcomes_against_the_matched_cell(tmp_path):
    uni, lab = [], []
    for i in range(10):    # one month, ten eligible companies, two liquidity buckets
        cik = str(i)
        uni.append({"month": "2015-01-01", "cik": cik, "ticker": "T" + cik, "code": cik, "sec_type": "CS",
                    "price": 10.0, "adv60": 1e6, "mom": float(i), "eligible": True, "size_q": 1 + i % 2})
        for y in (1, 3, 5):
            lab.append({"month": "2015-01-01", "cik": cik, "years": y, "status": "complete", "ret": 0.0,
                        "cagr": 0.0, "excess": 0.5 if i == 9 else -0.1, "max_mult": 6.0 if i == 9 else 1.2,
                        "min_mult": 0.9, "max_dd": 0.1, "d2": None, "d5": None, "d10": None,
                        "perm_loss": i == 0, "entry_day": 0, "end_day": 1})
    pq.write_table(pa.Table.from_pylist(uni), tmp_path / "universe.parquet")
    pq.write_table(pa.Table.from_pylist(lab), tmp_path / "labels.parquet")
    con = dl.open_db(tmp_path)
    rows, cells, months_ = dl.outcome_tables(con, ["2015-01-01"])
    assert rows[("2015-01-01", "9", 3)]["mom_q"] == 5 and months_[("2015-01-01", 3)]["n"] == 10
    m = dl.measure([{"month": "2015-01-01", "cik": "9"}], rows, cells, months_, 5)
    assert m["n"] == 1 and m["p_beat"] == 1.0 and m["p5"] == 1.0
    assert m["universe"]["p_beat"] == pytest.approx(0.1) and m["matched"]["p_beat"] == pytest.approx(1.0)
    assert dl.measure([{"month": "2015-01-01", "cik": "nope"}], rows, cells, months_, 5) == {"n": 0}


def test_run_end_to_end(tmp_path, capsys):
    test_outcomes_against_the_matched_cell(tmp_path)
    grades = [g("p9", "9", "2015-01-01", ticker="ASML", prompt_version="thesis_v2.x"),
              g("p1", "1", "2015-01-01", argument_quality=10)]
    pq.write_table(pa.Table.from_pylist(grades), tmp_path / "thesis_threads_identified.thesis_v2.parquet")
    assert dl.main(["run", "--features", str(tmp_path), "--prompt", "thesis_v2", "--inspect", "ASML", "RKLB",
                    "--titles", str(tmp_path / "none"), "--mentions", str(tmp_path / "none"),
                    "--out", str(tmp_path / "d.json")]) == 0
    out = capsys.readouterr().out
    assert "level used" in out and "-- ASML: 1 graded threads" in out and "DARLING" in out
    assert "-- RKLB: 0 graded threads" in out and "2015-01 ASML" in out and (tmp_path / "d.json").exists()


def test_crowd_rule_any_point_in_time_and_pushback_later_does_not_undo_it():
    lv = dl.Crowd("t", bulls=2, authors=2, comments=30, credibility=4.0)
    c = dict(credibility=5.0)
    gs = [g("a", "1", "2021-01-01", **c), g("b", "1", "2021-02-01", **c),            # two bulls, two authors
          g("c", "1", "2021-05-01", stance=0.8, direction_5=1, **c),                   # later pushback
          g("d", "1", "2021-05-01", stance=-0.8, direction_5=1, **c),
          g("e", "2", "2021-01-01", **c), g("f", "2", "2021-01-01", author="ae", **c),  # one author twice
          g("h", "3", "2021-01-01", **c), g("j", "3", "2021-01-01", stance=-0.5, direction_5=2, **c),
          g("i", "3", "2021-02-01", **c), g("k", "3", "2021-02-01", stance=-0.5, direction_5=1, **c)]  # bears >= bulls
    ev = dl.crowd_events(gs, lv, (2019, 2024))
    assert [(e["cik"], e["month"]) for e in ev] == [("1", "2021-02-01")]
    assert ev[0]["bulls"] == 2 and ev[0]["authors"] == 2 and ev[0]["threads"] == ["a", "b"]
    assert dl.crowd_events(gs, lv, (2019, 2024), exclude={("2021-02-01", "1")}) == []   # a blue chip then
    w = dl.crowd_window(gs[6:], dl.month_index("2021-02-01"))
    assert w["bulls"] == 2 and w["bears"] == 2 and "bear weight 10.0 >= bull weight 10.0" in dl.crowd_failures(w, lv)
    # a bullish post whose comments pushed it to neutral still counts as bullish; bearish direction does not
    assert dl.bull_thread(g("x", "1", "2021-01-01", direction_5=3)) and not dl.bull_thread(g("x", "1", "2021-01-01", direction_5=2))
    # the value can be all in the comments: a question post (stance 0) whose thread points up counts
    assert dl.bull_thread(g("x", "1", "2021-01-01", stance=0.0, direction_5=4))
    assert not dl.bull_thread(g("x", "1", "2021-01-01", stance=0.0, direction_5=3))


def test_only_credible_bears_count_and_by_weight():
    lv = dl.Crowd("t", bulls=2, authors=2, comments=30, credibility=4.0)
    gs = [g("a", "1", "2021-01-01", credibility=6.0), g("b", "1", "2021-01-01", credibility=6.0),
          *[g(f"w{i}", "1", "2021-01-01", stance=-0.8, direction_5=1, credibility=2.0) for i in range(5)],  # weak bears
          g("s", "1", "2021-01-01", stance=-0.8, direction_5=1, credibility=8.0)]                          # one strong bear
    w = dl.crowd_window(gs, dl.month_index("2021-01-01"), lv.credibility)
    assert w["bears"] == 1 and w["bear_weight"] == 8.0 and w["bull_weight"] == 12.0 and not dl.crowd_failures(w, lv)
    weak_bull = g("c", "1", "2021-01-01", credibility=3.0)         # below the floor: does not count as a bull
    assert dl.crowd_window(gs + [weak_bull], dl.month_index("2021-01-01"), lv.credibility)["bulls"] == 2


def test_missed_runups_neither_count_nor_fire():
    lv = dl.Crowd("t", bulls=2, authors=2, comments=30, credibility=4.0)
    c = dict(credibility=5.0)
    gs = [g("a", "1", "2021-01-01", **c), g("b", "1", "2021-02-01", **c), g("d", "1", "2021-04-01", **c)]
    assert [e["month"] for e in dl.crowd_events(gs, lv, (2019, 2024))] == ["2021-02-01"]
    ran = {("2021-01-01", "1")}               # January's bull thread came after a double: it does not count
    assert [e["month"] for e in dl.crowd_events(gs, lv, (2019, 2024), ran=ran)] == ["2021-04-01"]
    ran2 = {("2021-02-01", "1")}              # no event in a run-up month, and its thread does not count later
    assert dl.crowd_events(gs, lv, (2019, 2024), ran=ran2) == []


def test_runups_from_the_price_series(tmp_path):
    import datetime as dt
    (tmp_path / "eod").mkdir()
    days = [dt.date(2020, 1, 1) + dt.timedelta(d) for d in range(0, 400)]
    close = [10.0 if d < dt.date(2020, 9, 1) else 25.0 for d in days]      # 2.5x in the six months to Jan 2021
    pq.write_table(pa.table({"date": [d.isoformat() for d in days], "open": close, "high": close, "low": close,
                             "close": close, "adjusted_close": close, "volume": [1e6] * len(days)}),
                   tmp_path / "eod" / "RUN.parquet")
    pq.write_table(pa.Table.from_pylist([{"month": "2021-01-01", "cik": "1", "code": "RUN"},
                                         {"month": "2020-08-01", "cik": "1", "code": "RUN"}]), tmp_path / "u.parquet")
    r = dl.runups(tmp_path / "u.parquet", tmp_path, {("2021-01-01", "1"), ("2020-08-01", "1"), ("2021-01-01", "9")})
    assert r[("2021-01-01", "1")] == pytest.approx(2.5) and r[("2020-08-01", "1")] == pytest.approx(1.0)
    assert ("2021-01-01", "9") not in r


def test_crowd_run_end_to_end(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dl, "MIN_PER_YEAR", 1)
    test_outcomes_against_the_matched_cell(tmp_path)
    grades = [g("p9", "9", "2015-01-01", ticker="RKLB", credibility=6.0, n_comments=30),
              g("q9", "9", "2015-01-01", ticker="RKLB", author="other", credibility=6.0, n_comments=30)]
    pq.write_table(pa.Table.from_pylist(grades), tmp_path / "thesis_threads_identified.thesis_v2.parquet")
    assert dl.main(["run", "--rule", "crowd", "--split", "dev", "--features", str(tmp_path), "--prompt", "thesis_v2",
                    "--inspect", "RKLB", "--titles", str(tmp_path / "none"), "--parquet", str(tmp_path / "none"),
                    "--blue-chip-top", "0", "--eodhd", str(tmp_path / "none"), "--out", str(tmp_path / "c.json")]) == 0
    out = capsys.readouterr().out
    assert "crowd darlings per year" in out and "DARLING" in out and "2015-01 RKLB" in out
    assert "C1 loose" in out and (tmp_path / "c.json").exists()
    res = json.loads((tmp_path / "c.json").read_text())
    assert res["events"][0]["health"]["status"] == "unknown" and set(res["by_health"]) == {"pass", "fail", "unknown"}
    assert res["watch_events"] == [] and "health gate UNKNOWN (1 darlings)" in out


def test_merge_grades_first_directory_wins(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    pq.write_table(pa.Table.from_pylist([g("p1", "1", "2015-01-01", stance=0.9)]), a / "thesis_threads_identified.thesis_v2.parquet")
    pq.write_table(pa.Table.from_pylist([g("p1", "1", "2015-01-01", stance=-0.9), g("p2", "1", "2015-02-01")]),
                   b / "thesis_threads_identified.thesis_v2.parquet")
    out = dl.merge_grades([a, b, tmp_path / "none"], "identified", "thesis_v2", log=lambda m: None)
    assert [(x["post_id"], x["stance"]) for x in out] == [("p1", 0.9), ("p2", 0.8)]
    with pytest.raises(SystemExit):
        dl.merge_grades([tmp_path / "none"], "identified", "thesis_v2", log=lambda m: None)


def test_name_check_drops_bare_tickers_that_mean_something_else(tmp_path):
    (tmp_path / "posts").mkdir()
    posts = [{"id": "p1", "title": "THC beverages are the future", "selftext": "cannabis drinks, THC everywhere"},
             {"id": "p2", "title": "Tenet Healthcare (THC) Q3", "selftext": "hospital margins"},
             {"id": "p3", "title": "THC", "selftext": "short squeeze?"},
             {"id": "p4", "title": "Thoughts on RKLB?", "selftext": "Rocket Lab's Neutron timeline"},
             {"id": "p5", "title": "BYD sales beat Tesla", "selftext": "chinese EVs"},
             {"id": "p6", "title": "AMD earnings", "selftext": "MI300 ramp"}]
    pq.write_table(pa.Table.from_pylist(posts), tmp_path / "posts" / "stocks.parquet")
    pq.write_table(pa.Table.from_pylist([{"post_id": "p3", "cik": "1", "method": "cashtag"}]), tmp_path / "m.parquet")
    gs = [g("p1", "1", "2021-01-01", ticker="THC", name="Tenet Healthcare Corporation"),
          g("p2", "1", "2021-01-01", ticker="THC", name="Tenet Healthcare Corporation"),
          g("p3", "1", "2021-01-01", ticker="THC", name="Tenet Healthcare Corporation"),
          g("p4", "2", "2024-04-01", ticker="RKLB", name="Rocket Lab USA, Inc."),     # unambiguous
          g("p5", "3", "2024-04-01", ticker="BYD", name="Boyd Gaming Corp"),
          g("p9", "3", "2024-04-01", ticker="BYD", name="Boyd Gaming Corp"),         # not in the corpus: kept
          g("p6", "4", "2024-04-01", ticker="AMD", name="Advanced Micro Devices")]   # unambiguous: kept bare
    keep, dropped = dl.name_check(gs, tmp_path, tmp_path / "m.parquet", tmp_path / "massive",
                                  frozenset({"gaming"}), log=lambda m: None)
    assert sorted(x["post_id"] for x in dropped) == ["p1", "p5"]           # cannabis THC, the BYD carmaker
    assert sorted(x["post_id"] for x in keep) == ["p2", "p3", "p4", "p6", "p9"]  # named, cashtag, ok, AMD, unknown
    assert dl.ambiguous("thc", frozenset()) and not dl.ambiguous("NVDA", frozenset()) and dl.ambiguous("COST", frozenset({"cost"}))
    words, phrases = dl.name_words(["SP Plus Corp"], frozenset({"plus"}))
    assert words == set() and phrases == {"sp plus"}
