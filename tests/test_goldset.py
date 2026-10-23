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
    assert flat["is_thesis_p"] is None                                   # a chat model gives none
    rec["result"]["_jev"] = {"is_thesis_p": 0.83}
    assert tl.flatten(rec)["is_thesis_p"] == 0.83
    assert set(tl.GRADE_FIELDS) <= set(flat) and set(tl.COMMENT_FIELDS) <= set(flat)
    assert flat["credibility"] is None and flat["credibility_10"] is None     # a thesis_v1 grade
    # every flattened field has a column in the graded schema
    names = set(tl.GRADED_SCHEMA.names)
    assert set(flat) <= names and "prompt" in names
    # a thesis_v2 grade from a chat model (integers, no levels) and from Jev (levels already there)
    rec["result"].update({"direction": 4, "credibility": 7})
    flat = tl.flatten(rec)
    assert flat["direction_5"] == 4 and flat["credibility_10"] == 7 and flat["credibility"] == 7
    assert "direction_5" not in rec["result"]                                  # flatten does not mutate the cache
    rec["result"].update({"direction": 3.6, "credibility": 7.4, "direction_5": 5, "credibility_10": 8})
    flat = tl.flatten(rec)
    assert flat["direction_5"] == 5 and flat["credibility_10"] == 8 and flat["direction"] == 3.6


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


def test_build_and_evaluate_end_to_end_with_the_fake_model(tmp_path, monkeypatch, capsys):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import test_thesis_funnel as ff
    import thesis_funnel as tf
    t = ff.T1 - 20 * ff.DAY
    posts = [ff.post("a", t, 3000, score=50), ff.post("b", t + ff.DAY, 2500, score=5),
             ff.post("c", t + 2 * ff.DAY, 2000, score=0)]
    mentions = [ff.mention(p["id"], p["created_utc"], cik=str(i + 1)) for i, p in enumerate(posts)]
    comments = [{"id": f"c{i}", "post_id": "a", "parent_id": "t3_a", "created_utc": t + 3600 * i,
                 "author": f"u{i}", "body": "a substantive comment with a number: 42", "score": 9 - i,
                 "edited_utc": 0} for i in range(3)]
    ff.build(tmp_path, posts, mentions, [ff.feat(p) for p in posts], comments)
    corpus = tl.open_corpus(type("A", (), {"parquet": tmp_path, "mentions": tmp_path / "mentions.parquet",
                                           "post_features": tmp_path / "pf.parquet", "maturation": 0})())
    threads, _ = tf.plan(corpus, [ff.M1], {ff.M1: {"1": {}, "2": {}, "3": {}}}, lookback=90, log=lambda *_: None)
    pq.write_table(pa.Table.from_pylist(threads, schema=tf.THREAD_SCHEMA), tmp_path / "threads.parquet")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.chdir(tmp_path)   # keep the (no-credentials) bucket push from touching the repo
    common = ["--threads", str(tmp_path / "threads.parquet"), "--gold", str(tmp_path / "gold"),
              "--llm-dir", str(tmp_path / "llm"), "--parquet", str(tmp_path),
              "--mentions", str(tmp_path / "mentions.parquet"), "--post-features", str(tmp_path / "pf.parquet")]
    assert gs.main([*common, "build", "--n", "3"]) == 0
    rows = list(csv.DictReader((tmp_path / "gold" / "ratings.csv").open()))
    assert len(rows) == 3 and (tmp_path / "gold" / "threads.md").read_text().count("## ") == 3
    for r, rating in zip(rows, ("9", "4", "")):           # two rated, one left blank
        r["rating"], r["scale"] = rating, "10" if rating else ""
    with (tmp_path / "gold" / "ratings.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    capsys.readouterr()
    assert gs.main([*common, "evaluate", "--ratings", str(tmp_path / "gold" / "ratings.csv")]) == 0
    out = capsys.readouterr().out
    monkeypatch.setenv("LLM_MODEL", "gpt-5-nano")      # --model wins over the environment
    assert gs.main([*common, "evaluate", "--ratings", str(tmp_path / "gold" / "ratings.csv"), "--model", "fake"]) == 0
    assert "model fake" in capsys.readouterr().out
    assert "2 rated threads" in out and "Spearman rho" in out and "n_comments" in out and "voices" in out
    ev = list(csv.DictReader((tmp_path / "gold" / "eval_fake.csv").open()))
    assert len(ev) == 2 and {e["you"] for e in ev} == {"9.0", "4.0"} and all(e["Q"] for e in ev)
    assert "credibility" not in ev[0] and "direct credibility" not in out
    # the second prompt: its own gold cache, its own CSV, the direct credibility in the output
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("THESIS_PROMPT", "thesis_v2")
    assert gs.main([*common, "evaluate", "--ratings", str(tmp_path / "gold" / "ratings.csv"), "--tune"]) == 0
    out2 = capsys.readouterr().out
    assert "prompt thesis_v2." in out2 and "direct credibility (the model's own 1-10) rho" in out2
    assert "direct (the model's own credibility, thesis_v2)" in out2 and "dir cred" in out2
    ev2 = list(csv.DictReader((tmp_path / "gold" / "eval_fake.thesis_v2.csv").open()))
    assert len(ev2) == 2 and all(1 <= float(e["credibility"]) <= 10 and 1 <= float(e["direction"]) <= 5 for e in ev2)
    assert len(list(csv.DictReader((tmp_path / "gold" / "eval_fake.csv").open()))) == 2   # v1's file untouched
    v2_cache = [d for d in (tmp_path / "llm" / "cache" / "fake").iterdir() if d.name.startswith("thesis_v2.")]
    assert len(v2_cache) == 1 and (v2_cache[0] / "gold-why").is_dir()
    # grading and collecting under the second prompt writes the suffixed grades file only
    monkeypatch.delenv("THESIS_PROMPT")
    llm_common = ["--threads", str(tmp_path / "threads.parquet"), "--features", str(tmp_path / "feat"),
                  "--llm-dir", str(tmp_path / "llm"), "--parquet", str(tmp_path),
                  "--mentions", str(tmp_path / "mentions.parquet"), "--post-features", str(tmp_path / "pf.parquet")]
    (tmp_path / "feat").mkdir()
    assert tl.main([*llm_common, "--prompt", "thesis_v2", "run", "--variant", "identified"]) == 0
    assert tl.main([*llm_common, "--prompt", "thesis_v2", "collect", "--variant", "identified"]) == 0
    import pyarrow.parquet as pq
    assert not (tmp_path / "feat" / "thesis_threads_identified.parquet").exists()
    got = pq.read_table(tmp_path / "feat" / "thesis_threads_identified.thesis_v2.parquet").to_pylist()
    assert len(got) == 3 and all(1 <= r["credibility_10"] <= 10 and r["prompt"] == "thesis_v2" for r in got)
    assert all(r["prompt_version"].startswith("thesis_v2.") for r in got)
    assert "thesis_threads_identified.thesis_v2.parquet" in capsys.readouterr().out


def test_coverage_and_min_comments(tmp_path):
    rows = [dict(thread(i, 2013 + i % 3, "stocks", 1000, i), n_comments=i) for i in range(12)]
    text = gs.coverage(rows[:3], rows)
    assert "gold set 3 of 12" in text and ">= 10 comments: gold 0%, planned 17%" in text and "2013 1" in text
    f = tmp_path / "r.csv"
    f.write_text("n,post_id,cik,ticker,title,rating,reason,scale\n1,p0,0,T0,t,5,,10\n")
    picked, _ = gs.keep_rows(rows, f, 4, 0, log=lambda *_: None, min_comments=9)
    assert picked[0]["post_id"] == "p0" and all(r["n_comments"] >= 9 for r in picked[1:]) and len(picked) == 4
