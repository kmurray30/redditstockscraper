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
    code, grid = get(server + "/api/grid?q=50")
    counts = json.loads(grid)["counts"]
    assert code == 200 and counts["bear"]["1"] == 1 and counts["bull"]["0"] == 1 and counts["neutral"]["1"] == 1
    code, html_ = get(server + "/?q=50")
    assert code == 200 and "/list?stance=bear&amp;sub=1&amp;q=50" in html_.replace("&sub", "&amp;sub") or "/list?stance=bear&sub=1&q=50" in html_
    code, lst = get(server + "/list?stance=bear&sub=1&q=50")
    assert code == 200 and "Killing Me Softly" in lst and "/post/p1_1" in lst and "reddit.com" not in lst
    code, post = get(server + "/post/p1_1")
    assert code == 200 and "&lt;b&gt;Sell&lt;/b&gt;" in post and "AWS alone" in post and "x5.9" in post
    assert "+414%" in post and "stance" in post and "argument quality" in post
    assert get(server + "/post/nope")[0] == 404 and get(server + "/list?stance=up&sub=1")[0] == 404
    code, post2 = get(server + "/post/p2_1")
    assert code == 200 and "not in the export" in post2


def test_stance_classes_and_grid_threshold():
    assert tsv.stance_class(0.3) == "bull" and tsv.stance_class(-0.25) == "bear" and tsv.stance_class(0.1) == "neutral"
    store = tsv.Store([{"key": "a", "stance": 1, "q": 55}, {"key": "b", "stance": 1, "q": 45}])
    assert store.grid(50)["counts"]["bull"] == {"1": 1, "0": 1} and store.grid(40)["counts"]["bull"] == {"1": 2, "0": 0}
