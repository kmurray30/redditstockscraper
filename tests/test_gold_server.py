import base64
import csv
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import gold_page as gp  # noqa: E402
import gold_server as gs  # noqa: E402

MSG = ("AS-OF DATE: 2015-03-04 (UTC). Use nothing from after this date.\n"
       "COMPANY UNDER EVALUATION: Acme Widgets Inc (ACME)\n"
       "SUBREDDIT: r/stocks   FLAIR: DD   POST VOTES: 42\n\n"
       "TITLE: Why ACME doubles\n\nBODY:\nAcme ships widgets. Margins are up.\n\n"
       "COMMENTS (2 shown of 3; [votes] label: text; '>' marks a reply):\n"
       "[9] u1: The 10-K says otherwise\n  > [3] OP: Fair point, see note 4\n")
THREADS_MD = ("# Gold set\n\nintro\n\n---\n\n## 1. [ACME] Why ACME doubles\n\n"
              "post_id: `p1`   company: Acme Widgets Inc\n\n```text\n" + MSG + "\n```\n"
              "\n\n---\n\n## 2. [BETA] Beta is cheap\n\npost_id: `p2`   company: Beta Corp\n\n```text\n"
              + MSG.replace("ACME", "BETA").replace("Acme Widgets Inc", "Beta Corp") + "\n```\n")
RATINGS_CSV = ("n,post_id,cik,ticker,title,rating,reason\n"
               "1,p1,100,ACME,Why ACME doubles,,\n2,p2,200,BETA,Beta is cheap,,\n")


class FakeBucket:
    def __init__(self):
        self.objects = {"data/gold/threads.md": THREADS_MD.encode(),
                        "data/gold/ratings.csv": RATINGS_CSV.encode()}

    def get_bytes(self, key):
        return self.objects[key]

    def put_bytes(self, key, data):
        self.objects[key] = data


@pytest.fixture
def server():
    bucket = FakeBucket()
    store = gs.Store(bucket)
    store.load()
    httpd = gs.serve(store, "asdf", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", bucket, store
    httpd.shutdown()


def call(url, path, password=None, data=None):
    req = urllib.request.Request(url + path, data=data, method="POST" if data else "GET")
    if password is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{password}".encode()).decode())
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_page_parses_and_builds_in_server_mode():
    threads = gp.parse_threads_md(THREADS_MD)
    gp.attach_ciks(threads, RATINGS_CSV)
    assert [t["ticker"] for t in threads] == ["ACME", "BETA"] and threads[0]["cik"] == "100"
    assert threads[0]["comments"][1]["label"] == "OP" and threads[0]["votes"] == 42
    page = gp.build(threads, mode="server")
    assert "__GOLD_API__ = true" in page and "Why ACME doubles" in page
    assert "__GOLD_API__ = true" not in gp.build(threads)
    with pytest.raises(ValueError):
        gp.build(threads, mode="nope")


def test_password_gate(server):
    url, bucket, store = server
    assert call(url, "/healthz")[0] == 200                       # open, no data
    code, _, headers = call(url, "/")
    assert code == 401 and "Basic" in headers.get("WWW-Authenticate", "")
    assert call(url, "/", "wrong")[0] == 401
    assert call(url, "/api/ratings", "wrong")[0] == 401
    assert call(url, "/api/rate", "wrong", b'{"post_id":"p1","rating":5}')[0] == 401
    code, body, headers = call(url, "/", "asdf")
    assert code == 200 and b"Gold set ratings" in body and b"__GOLD_API__" in body
    assert headers.get("Cache-Control") == "no-store" and "noindex" in headers.get("X-Robots-Tag", "")
    assert call(url, "/nope", "asdf")[0] == 404


def test_rating_round_trip_rewrites_the_bucket_csv(server):
    url, bucket, store = server
    code, body, _ = call(url, "/api/ratings", "asdf")
    assert code == 200 and json.loads(body) == {}
    code, body, _ = call(url, "/api/rate", "asdf",
                         json.dumps({"post_id": "p2", "rating": 4, "reason": "cites the filing"}).encode())
    assert code == 200 and json.loads(body)["saved"]["rating"] == 4
    rows = list(csv.DictReader(io.StringIO(bucket.objects["data/gold/ratings.csv"].decode())))
    assert rows[1]["rating"] == "4" and rows[1]["reason"] == "cites the filing" and rows[1]["ts"]
    assert rows[0]["rating"] == ""
    code, body, _ = call(url, "/api/ratings", "asdf")
    assert json.loads(body)["p2"]["rating"] == 4 and "p1" not in json.loads(body)
    # re-rating overwrites
    call(url, "/api/rate", "asdf", json.dumps({"post_id": "p2", "rating": 2, "reason": ""}).encode())
    assert json.loads(call(url, "/api/ratings", "asdf")[1])["p2"]["rating"] == 2
    # a fresh store loads the saved ratings back from the bucket
    s2 = gs.Store(bucket)
    s2.load()
    assert s2.ratings()["p2"]["rating"] == 2


def test_bad_requests_are_refused(server):
    url, bucket, store = server
    assert call(url, "/api/rate", "asdf", json.dumps({"post_id": "p1", "rating": 11}).encode())[0] == 400
    assert call(url, "/api/rate", "asdf", json.dumps({"post_id": "p1", "rating": 0}).encode())[0] == 400
    assert call(url, "/api/rate", "asdf", json.dumps({"post_id": "zz", "rating": 3}).encode())[0] == 400
    assert call(url, "/api/rate", "asdf", b"not json")[0] == 400
    assert bucket.objects["data/gold/ratings.csv"] == RATINGS_CSV.encode()


def test_ten_point_scale_and_old_five_point_rows_are_converted(server):
    url, bucket, store = server
    code, body, _ = call(url, "/api/rate", "asdf", json.dumps({"post_id": "p1", "rating": 10}).encode())
    assert code == 200
    rows = list(csv.DictReader(io.StringIO(bucket.objects["data/gold/ratings.csv"].decode())))
    assert rows[0]["rating"] == "10" and rows[0]["scale"] == "10"
    old = ("n,post_id,cik,ticker,title,rating,reason,ts\n"
           "1,p1,100,ACME,Why ACME doubles,4,ok,x\n2,p2,200,BETA,Beta is cheap,,,\n")
    b2 = FakeBucket()
    b2.objects["data/gold/ratings.csv"] = old.encode()
    s2 = gs.Store(b2)
    s2.load()
    assert s2.ratings()["p1"]["rating"] == 8 and "p2" not in s2.ratings()
    assert [gp.to_ten({"rating": str(r)})["rating"] for r in range(1, 6)] == ["1", "3", "6", "8", "10"]
    assert gp.to_ten({"rating": "8", "scale": "10"})["rating"] == "8"
