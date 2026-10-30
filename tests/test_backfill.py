import sys
from pathlib import Path

import pyarrow.parquet as pq
import zstandard as zstd
import json

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import corpus  # noqa: E402
import download_reddit as dr  # noqa: E402


def test_comments_only_for_threads_the_darling_pipeline_can_use():
    assert dr.wants_comments({"is_self": True, "selftext": "x" * 400})
    assert not dr.wants_comments({"is_self": True, "selftext": "Thoughts on RKLB?", "num_comments": 3})
    assert dr.wants_comments({"is_self": True, "selftext": "Thoughts on RKLB?", "num_comments": 262})
    assert dr.wants_comments({"is_self": False, "url": "https://www.sec.gov/x.htm", "title": "10-K"})
    assert not dr.wants_comments({"is_self": False, "url": "https://youtube.com/x", "title": "rocket go up"})


def write_zst(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstd.ZstdCompressor().compress(b"\n".join(json.dumps(r).encode() for r in rows) + b"\n"))


def test_arctic_months_become_sub__arctic_parquet(tmp_path):
    src, out = tmp_path / "arctic", tmp_path / "parquet"
    for m, n in (("2025-01", 2), ("2025-02", 1), ("2025-03", 5)):
        write_zst(src / "stocks" / f"posts_{m}.zst",
                  [{"id": f"{m}-{i}", "subreddit": "stocks", "created_utc": 1735689600 + i, "title": "t", "selftext": "b"}
                   for i in range(n)])
        write_zst(src / "stocks" / f"comments_{m}.zst",
                  [{"id": f"c{m}", "link_id": f"t3_{m}-0", "created_utc": 1735689601, "body": "x"}])
    for m in ("2025-01", "2025-02"):                    # March is not finished: no mentions file
        (src / "stocks" / f"mentions_{m}.csv").write_text("ticker,n\n")
    counts = corpus.build_arctic(src, out, log=lambda m: None)
    assert counts[("stocks", "posts")] == 3 and counts[("stocks", "comments")] == 2
    posts = pq.read_table(out / "posts" / "stocks__arctic.parquet").to_pylist()
    assert {p["id"] for p in posts} == {"2025-01-0", "2025-01-1", "2025-02-0"}
    assert pq.read_table(out / "comments" / "stocks__arctic.parquet").to_pylist()[0]["post_id"] == "2025-01-0"
    assert corpus.build_arctic(src, out, log=lambda m: None) == {}          # up to date: nothing rewritten
    assert corpus.build_arctic(tmp_path / "none", out) == {}
