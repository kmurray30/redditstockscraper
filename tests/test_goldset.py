import csv
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import goldset as gs  # noqa: E402
import thesis_llm as tl  # noqa: E402


def thread(i, year, sub, chars, votes, n_companies=1):
    return {"post_id": f"p{i}", "cik": str(i), "ticker": f"T{i}", "first_month": f"{year}-03-01",
            "subreddit": sub, "chars": chars, "votes": votes, "n_companies": n_companies}


def test_sample_is_single_company_and_spread_across_strata():
    rows = []
    i = 0
    for year in (2013, 2015, 2018):
        for sub in ("stocks", "investing"):
            for chars in (500, 5000):
                for votes in (1, 500):
                    for _ in range(4):
                        rows.append(thread(i, year, sub, chars, votes))
                        i += 1
    rows.append(thread(999, 2015, "stocks", 5000, 500, n_companies=2))
    picked = gs.sample(rows, 30, seed=1)
    assert len(picked) == 30 and all(r["n_companies"] == 1 for r in picked)
    assert len({r["post_id"] for r in picked}) == 30
    years = Counter(r["first_month"][:4] for r in picked)
    subs = Counter(r["subreddit"] for r in picked)
    assert min(years.values()) >= 8 and min(subs.values()) >= 12   # 24 cells, round-robin
    assert gs.sample([thread(1, 2015, "s", 100, 1, n_companies=3)], 5, 0) == []
    assert gs.sample(rows, 30, seed=1) == picked                     # deterministic


def test_ratings_and_spearman(tmp_path):
    f = tmp_path / "ratings.csv"
    with f.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["n", "post_id", "cik", "ticker", "title", "rating", "reason", "scale"])
        w.writeheader()
        w.writerow({"n": 1, "post_id": "a", "cik": "1", "ticker": "A", "title": "t", "rating": "4", "reason": "ok",
                    "scale": "10"})
        w.writerow({"n": 2, "post_id": "b", "cik": "2", "ticker": "B", "title": "t", "rating": "", "reason": ""})
    r = gs.read_ratings(f)
    assert list(r) == [("a", "1")] and r[("a", "1")]["rating"] == 4.0
    assert gs.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert gs.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert gs.spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)
    assert gs.spearman([1, 2], [1, 2]) != gs.spearman([1, 2], [1, 2])   # nan for n < 3


def test_flatten_and_collect_shapes(tmp_path):
    rec = {"result": {"is_thesis": True, "stance": 0.5, "horizon": "years", "argument_quality": 70,
                      "evidence_quality": 60, "specificity": 50, "tone_stability": 80,
                      "falsifiability": 40, "balance": 30, "hype_risk": 10, "confidence": 0.8,
                      "comments": {"independent_support": 20, "independent_opposition": 5,
                                   "strongest_objection_severity": 15, "n_independent_voices": 2}},
           "model": "fake", "cost": 0.0001}
    flat = tl.flatten(rec)
    assert flat["argument_quality"] == 70 and flat["independent_support"] == 20 and flat["model"] == "fake"
    assert set(tl.GRADE_FIELDS) <= set(flat) and set(tl.COMMENT_FIELDS) <= set(flat)
    # every flattened field has a column in the graded schema
    names = set(tl.GRADED_SCHEMA.names)
    assert set(flat) <= names


def test_load_threads_filters_years_and_limits(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [{"post_id": f"p{i}", "cik": "1", "first_month": f"{2012 + i % 3}-01-01"} for i in range(9)]
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "t.parquet")
    assert len(tl.load_threads(tmp_path / "t.parquet", years=[2013])) == 3
    assert len(tl.load_threads(tmp_path / "t.parquet", limit=4)) == 4
    assert [r["first_month"] for r in tl.load_threads(tmp_path / "t.parquet")][:3] == ["2012-01-01"] * 3


def test_keep_rows_carries_an_existing_set_over(tmp_path):
    rows = [thread(i, 2015, "stocks", 1000, 5) for i in range(10)]
    f = tmp_path / "ratings.csv"
    with f.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["n", "post_id", "cik", "ticker", "title", "rating", "reason", "ts"])
        w.writeheader()
        w.writerow({"n": 1, "post_id": "p3", "cik": "3", "ticker": "T3", "title": "t", "rating": "4", "reason": "ok", "ts": "x"})
        w.writerow({"n": 2, "post_id": "gone", "cik": "99", "ticker": "T99", "title": "t", "rating": "2", "reason": "", "ts": ""})
        w.writerow({"n": 3, "post_id": "p1", "cik": "1", "ticker": "T1", "title": "t", "rating": "", "reason": "", "ts": ""})
    logs = []
    picked, old = gs.keep_rows(rows, f, 4, 0, log=logs.append)
    assert [r["post_id"] for r in picked[:2]] == ["p3", "p1"]      # kept, in the CSV's order
    assert len(picked) == 4 and len({r["post_id"] for r in picked}) == 4
    assert "gone" not in {r["post_id"] for r in picked} and "T99 gone" in logs[0]
    assert old[("p3", "3")]["rating"] == "8" and old[("p3", "3")]["scale"] == "10"   # 1-5 row converted
