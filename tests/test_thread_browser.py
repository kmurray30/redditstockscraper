import base64
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import thread_export as te  # noqa: E402
import thread_server as tsv  # noqa: E402

MSG = ("AS-OF DATE: 2015-08-01 (UTC). Use nothing from after this date.\n"
       "COMPANY UNDER EVALUATION: Amazon.com (AMZN)\nSUBREDDIT: r/investing   POST VOTES: 231\n\n"
       "TITLE: Amazon Stock Price: Killing Me Softly\n\nBODY:\nIt trades at 1,000x earnings. <b>Sell</b>.\n\n"
       "COMMENTS (3 of 40 shown, best voted first):\n[50] u1: AWS alone is worth it.\n>[12] OP: Not at this price.\n[3] u2: agreed\n")


def grade(post_id="p1", cik="1", stance=-1.0, q=80, **kw):
    g = {"post_id": post_id, "cik": cik, "ticker": "AMZN", "name": "Amazon.com", "subreddit": "investing",
         "created_utc": 1437696000, "first_month": "2015-08-01", "votes": 231, "n_comments": 40, "n_companies": 1,
         "late_shown": 0, "edited_late": False, "is_thesis": True, "is_thesis_p": 0.9, "stance": stance,
         "horizon": "years", "confidence": 0.8, "argument_quality": q, "evidence_quality": q, "specificity": q,
         "tone_stability": q, "falsifiability": q, "balance": q, "hype_risk": 0, "independent_support": 50,
         "independent_opposition": 50, "strongest_objection_severity": 50, "n_independent_voices": 3,
         "model": "jev-1.13.0", "prompt_version": "thesis_v1.x.b2.jevabc123"}
    g.update(kw)
    return g


def test_export_reads_the_cached_text_and_attaches_the_outcome(tmp_path):
    import llm
    g = grade()
    cache = llm.Cache(llm.cache_dir(tmp_path, g["model"], g["prompt_version"], "identified"))
    cache.root.mkdir(parents=True)
    cache.path("p1_1").write_text(json.dumps({"user": MSG, "result": {}}))
    rows = te.export([g, grade(post_id="p2", stance=1.0, q=20)], tmp_path, "identified",
                     {("2015-08-01", "1"): {"status": "complete", "max_mult": 5.9, "excess": 4.14, "perm_loss": False, "cagr": 0.4}},
                     log=lambda m: None)
    assert rows[0]["title"] == "Amazon Stock Price: Killing Me Softly" and rows[0]["body"].startswith("It trades")
    assert [c["label"] for c in rows[0]["comments"]] == ["u1", "OP", "u2"] and rows[0]["comments"][1]["depth"] == 1
    assert rows[0]["outcome"]["max_mult"] == 5.9 and rows[0]["q"] > rows[1]["q"] and not rows[0]["text_missing"]
    assert rows[1]["text_missing"] and rows[1]["title"] == "" and rows[1]["outcome"] == rows[0]["outcome"]


@pytest.fixture
def server():
    rows = [te.record(grade(), MSG, {"status": "complete", "max_mult": 5.9, "excess": 4.14, "perm_loss": False, "cagr": 0.4}, __import__("thesis_scorer").Params()),
            te.record(grade(post_id="p2", stance=1.0, q=20), None, None, __import__("thesis_scorer").Params()),
            te.record(grade(post_id="p3", stance=0.1, q=90), None, None, __import__("thesis_scorer").Params())]
    data = zstd.ZstdCompressor().compress(json.dumps(rows).encode())
    store = tsv.Store.from_bytes(data)
    httpd = tsv.serve(store, "pw", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def get(url, password="pw"):
    req = urllib.request.Request(url)
    if password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_pages_need_the_password_and_render_grid_list_and_post(server):
    assert get(server + "/healthz", password=None)[0] == 200
    assert get(server + "/", password=None)[0] == 401 and get(server + "/", password="wrong")[0] == 401
    code, grid = get(server + "/api/grid?measure=q")
    g = json.loads(grid)
    counts = g["counts"]
    assert code == 200 and g["measure"] == "q" and len(g["bins"]) == 10
    assert sum(counts["strong_bear"]) == 1 and sum(counts["strong_bull"]) == 1 and sum(counts["neutral"]) == 1
    assert counts["strong_bear"][tsv.qbin(74.0)] == 1 and counts["strong_bull"][tsv.qbin(24.0)] == 1
    code, html_ = get(server + "/")
    assert code == 200 and "/list?stance=strong_bear&qbin=" in html_ and "Strong bull" in html_
    code, lst = get(server + f"/list?stance=strong_bear&qbin={tsv.qbin(74.0)}&measure=q")
    assert code == 200 and "Killing Me Softly" in lst and "/post/p1_1" in lst and "reddit.com" not in lst
    code, post = get(server + "/post/p1_1")
    assert code == 200 and "&lt;b&gt;Sell&lt;/b&gt;" in post and "AWS alone" in post and "x5.9" in post
    assert "+414%" in post and "stance" in post and "argument quality" in post
    assert get(server + "/post/nope")[0] == 404 and get(server + "/list?stance=up&qbin=1")[0] == 404
    assert get(server + "/list?stance=bull&qbin=12")[0] == 404
    code, post2 = get(server + "/post/p2_1")
    assert code == 200 and "not in the export" in post2


def test_stance_levels_quality_bins_and_the_measure_switch():
    assert tsv.stance_class(0.3) == "bull" and tsv.stance_class(0.9) == "strong_bull" and tsv.stance_class(-0.25) == "bear"
    assert tsv.stance_class(-0.8) == "strong_bear" and tsv.stance_class(0.1) == "neutral" and tsv.stance_class(None) == "neutral"
    assert tsv.qbin(0) == 0 and tsv.qbin(9.9) == 0 and tsv.qbin(55) == 5 and tsv.qbin(100) == 9 and tsv.qbin_label(9) == "90-100"
    store = tsv.Store([{"key": "a", "stance": 1, "q": 55, "q_gated": 25}, {"key": "b", "stance": 1, "q": 45}])
    assert store.measures == ["q", "q_gated"] and store.measure("nope") == "q"
    assert store.grid("q")["counts"]["strong_bull"][5] == 1 and store.grid("q")["counts"]["strong_bull"][4] == 1
    assert store.grid("q_gated")["counts"]["strong_bull"][2] == 1   # b has no q_gated: falls back to its q (45 -> bin 4)
    assert store.grid("q_gated")["counts"]["strong_bull"][4] == 1
    plain = tsv.Store([{"key": "a", "stance": 0, "q": 10}])
    assert plain.measures == ["q"] and plain.measure("q_gated") == "q" and plain.measure("direct") == "q"


def v2(post_id, stance, direction_5, credibility_10, **kw):
    g = grade(post_id=post_id, stance=stance, direction=float(direction_5), credibility=float(credibility_10),
              direction_5=direction_5, credibility_10=credibility_10, prompt="thesis_v2",
              prompt_version="thesis_v2.y.b2.jevdef456", variant="identified", **kw)
    return te.record(g, MSG if post_id == "p1" else None, None, __import__("thesis_scorer").Params())


def test_direct_grid_uses_the_model_s_own_levels(tmp_path):
    import thesis_scorer as ts
    rows = [v2("p1", -1.0, 4, 7), v2("p2", 1.0, 1, 10), v2("p3", 0.0, 3, 1),
            te.record(grade(post_id="p4", stance=0.9, q=50), None, None, ts.Params())]   # a v1 grade among them
    assert rows[0]["q_direct"] == pytest.approx((7 - 1) / 9 * 100, abs=0.05) and rows[0]["prompt"] == "thesis_v2"
    assert rows[3]["q_direct"] is None and rows[3]["credibility_10"] is None and rows[3]["prompt"] == "thesis_v1"
    store = tsv.Store.from_bytes(zstd.ZstdCompressor().compress(json.dumps(rows).encode()))
    assert store.measures == ["q", "q_gated", "direct"] and store.prompt == "thesis_v2"
    g = store.grid("direct")
    assert g["bins"] == [str(i) for i in range(1, 11)] and g["rows"] == "direction" and g["total"] == 3
    assert g["counts"]["bull"][6] == 1 and g["counts"]["strong_bear"][9] == 1 and g["counts"]["neutral"][0] == 1
    assert sum(sum(v) for v in g["counts"].values()) == 3          # the v1 grade is not in the direct grid
    assert [r["key"] for r in store.cell("bull", 6, "direct")] == ["p1_1"] and store.cell("strong_bull", 9, "direct") == []
    q = store.grid("q")
    assert q["rows"] == "stance" and sum(sum(v) for v in q["counts"].values()) == 4 and q["counts"]["strong_bear"][7] == 1
    httpd = tsv.serve(store, "pw", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, home = get(base + "/")                                    # the direct grid is the landing page
        assert code == 200 and "credibility &darr; direction &rarr;" in home and "prompt thesis_v2" in home and "/list?stance=bull&qbin=6&measure=direct" in home
        # 5 columns (strong bear .. strong bull) by 10 rows (credibility 10 at the top)
        heads = [home.index(f"<div class='head {k}'>") for k in ("strong_bear", "bear", "neutral", "bull", "strong_bull")]
        assert heads == sorted(heads) and heads[-1] < home.index("<div class=row>10</div>") < home.index("<div class=row>1</div>")
        assert home.count("<div class=row>") == 10 and home.count("<div class='cell") == 50
        code, lst = get(base + "/list?stance=bull&qbin=6&measure=direct")
        assert code == 200 and "Killing Me Softly" in lst and "cred 7/10" in lst and "dir 4/5" in lst and "credibility 7" in lst
        assert "Killing Me Softly" not in get(base + "/list?stance=strong_bear&qbin=6&measure=direct")[1]
        code, post = get(base + "/post/p1_1?measure=direct")
        assert code == 200 and "credibility (model, 1-10)" in post and "direction (model, 1-5)" in post and "4 (bull" in post
        assert "/list?stance=bull&qbin=6&measure=direct" in post and "thesis_v2" in post
        code, post4 = get(base + "/post/p4_1?measure=direct")           # a v1 record falls back to its q cell
        assert code == 200 and "credibility (model" not in post4 and "/list?stance=strong_bull&qbin=5&measure=q" in post4
        code, old = get(base + "/?measure=q")
        assert code == 200 and "quality &darr; stance &rarr;" in old and "/list?stance=strong_bull&qbin=5&measure=q" in old
        assert old.index("<div class=row>90-100</div>") < old.index("<div class=row>0-10</div>")
    finally:
        httpd.shutdown()
