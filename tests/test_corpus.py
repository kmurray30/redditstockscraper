import importlib.util
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

zstandard = pytest.importorskip("zstandard")

spec = importlib.util.spec_from_file_location(
    "corpus", Path(__file__).parents[1] / "scripts" / "corpus.py")
corpus = importlib.util.module_from_spec(spec)
sys.modules["corpus"] = corpus
spec.loader.exec_module(corpus)


def write_zst(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(data))


def make_sub(root, name="alpha"):
    d = root / name
    write_zst(d / "posts.zst", [
        {"id": "p1", "subreddit": name, "created_utc": "1577836800", "title": "AAPL up",
         "selftext": "text", "score": 5, "num_comments": 2, "link_flair_text": "DD",
         "matched_tickers": ["MSFT", "AAPL"], "is_self": True},
    ])
    write_zst(d / "comments.zst", [
        {"id": "c1", "link_id": "t3_p1", "parent_id": "t3_p1", "created_utc": "1577840000",
         "body": "agree", "score": 3, "author": "x"},
    ])
    (d / "manifest.json").write_text("{}")


def test_build_types_and_ids(tmp_path):
    make_sub(tmp_path / "src")
    corpus.build(tmp_path / "src", tmp_path / "out", log=lambda m: None)
    p = pq.read_table(tmp_path / "out" / "posts" / "alpha.parquet").to_pylist()[0]
    c = pq.read_table(tmp_path / "out" / "comments" / "alpha.parquet").to_pylist()[0]
    assert p["created_utc"] == 1577836800 and p["matched_tickers"] == ["AAPL", "MSFT"]
    assert p["flair"] == "DD"
    assert c["post_id"] == "p1" and c["created_utc"] == 1577840000


def test_build_skips_up_to_date_files(tmp_path):
    make_sub(tmp_path / "src")
    logs = []
    corpus.build(tmp_path / "src", tmp_path / "out", log=logs.append)
    logs.clear()
    corpus.build(tmp_path / "src", tmp_path / "out", log=logs.append)
    assert all("up to date" in m for m in logs) and len(logs) == 2


def test_non_string_fields_are_coerced(tmp_path):
    d = tmp_path / "src" / "beta"
    write_zst(d / "posts.zst", [{"id": 123, "subreddit": "beta", "created_utc": 1, "author": 7,
                                 "title": 5, "selftext": None, "link_flair_text": 9}])
    write_zst(d / "comments.zst", [{"id": 1, "link_id": 42, "created_utc": "x", "body": 3}])
    (d / "manifest.json").write_text("{}")
    corpus.build(tmp_path / "src", tmp_path / "out", log=lambda m: None)
    p = pq.read_table(tmp_path / "out" / "posts" / "beta.parquet").to_pylist()[0]
    c = pq.read_table(tmp_path / "out" / "comments" / "beta.parquet").to_pylist()[0]
    assert p["id"] == "123" and p["title"] == "5" and p["flair"] == "9" and p["selftext"] == ""
    assert c["id"] == "1" and c["created_utc"] == 0


def test_edited_timestamps():
    assert corpus.edited_utc(False) == 0 and corpus.edited_utc(None) == 0
    assert corpus.edited_utc(1577840000.0) == 1577840000
    assert corpus.edited_utc(True) == -1       # old dumps: edited at an unknown time
    assert corpus.edited_utc("garbage") == -1
