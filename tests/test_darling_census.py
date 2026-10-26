import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import darling_census as dc  # noqa: E402

T = 1_420_070_400   # 2015-01-01


def build(tmp, posts):
    (tmp / "posts").mkdir()
    pq.write_table(pa.Table.from_pylist([{"id": p["id"], "subreddit": "stocks", "created_utc": p["t"], "author": p["a"],
                                          "title": "t", "selftext": "", "score": 1, "upvote_ratio": 1.0,
                                          "num_comments": p["c"], "flair": None, "url": None, "domain": None,
                                          "is_self": True, "matched_tickers": [], "edited_utc": 0} for p in posts]),
                   tmp / "posts" / "stocks.parquet")
    pq.write_table(pa.Table.from_pylist([{"source": "post", "source_id": p["id"], "post_id": p["id"], "subreddit": "stocks",
                                          "created_utc": p["t"], "cik": cik, "ticker": tk, "sec_type": "CS",
                                          "method": "cashtag", "resolve_conf": "stable", "n": 1, "in_title": True}
                                         for p in posts for cik, tk in p["co"]]), tmp / "m.parquet")
    pq.write_table(pa.Table.from_pylist([{"post_id": p["id"], "f_bull": 0, "f_bear": 0, "f_sent": 0.0, "f_primary_links": 0,
                                          "f_news_links": 0, "f_links": 0, "f_chars": p.get("chars", 1000), "f_dd": False}
                                         for p in posts]), tmp / "f.parquet")


def test_census_counts_authors_over_the_window_and_cools_down(tmp_path, capsys):
    day = 86400
    rk = [("1", "RKLB")]
    posts = [{"id": "a", "t": T, "a": "x", "c": 60, "co": rk}, {"id": "b", "t": T + 40 * day, "a": "y", "c": 30, "co": rk},
             {"id": "c", "t": T + 70 * day, "a": "z", "c": 20, "co": rk},             # 3 authors, 110 comments by March
             {"id": "d", "t": T + 100 * day, "a": "x", "c": 500, "co": rk},           # inside the cooldown
             {"id": "e", "t": T, "a": "q", "c": 900, "co": [("2", "BIG")], "chars": 50},   # thin: not substantive
             {"id": "f", "t": T, "a": "r", "c": 900, "co": [("3", "A"), ("4", "B"), ("5", "C")]}]  # three companies
    build(tmp_path, posts)
    assert dc.main(["--parquet", str(tmp_path), "--mentions", str(tmp_path / "m.parquet"),
                    "--features", str(tmp_path / "f.parquet"), "--years", "2015", "2015",
                    "--list-level", "B", "--tickers", "RKLB", "ASML"]) == 0
    out = capsys.readouterr().out
    assert "2015:4" in out                                    # four substantive subject posts, all RKLB
    lines = {l.split()[0]: l for l in out.splitlines() if l.strip()[:1] in "ABCDE" and ">=" in l}
    assert lines["A"].split()[-1] == "1" and lines["B"].split()[-1] == "1" and lines["C"].split()[-1] == "0"
    assert "RKLB 03 3a/3p/110c" in out and "BIG" not in out and "-- ASML" in out
