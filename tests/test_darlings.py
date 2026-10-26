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
