import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "download_reddit", Path(__file__).parents[1] / "scripts" / "download_reddit.py"
)
dr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dr)


def benc(v) -> bytes:
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    if isinstance(v, dict):
        return b"d" + b"".join(benc(k) + benc(v[k]) for k in sorted(v)) + b"e"
    raise TypeError(v)


TORRENT = benc(
    {
        "announce": "http://tracker",
        "info": {
            "name": "reddit",
            "piece length": 16384,
            "pieces": b"\x00" * 20,
            "files": [
                {"length": 100, "path": ["subreddits24", "AskReddit_comments.zst"]},
                {"length": 5_000, "path": ["subreddits24", "stocks_comments.zst"]},
                {"length": 700, "path": ["subreddits24", "stocks_submissions.zst"]},
                {"length": 9_000, "path": ["subreddits24", "wallstreetbets_comments.zst"]},
            ],
        },
    }
)


def test_torrent_files_indices_are_one_based():
    files = dr.torrent_files(TORRENT)
    assert files[0] == (1, "subreddits24/AskReddit_comments.zst", 100)
    assert files[3][0] == 4


def test_select_files_case_insensitive_and_reports_missing():
    files = dr.torrent_files(TORRENT)
    chosen, missing = dr.select_files(files, ["Stocks", "WallStreetBets"], list(dr.KINDS))
    assert sorted(i for i, _, _ in chosen) == [2, 3, 4]
    assert missing == ["wallstreetbets_submissions.zst"]


def test_bdecode_rejects_trailing_garbage():
    with pytest.raises(ValueError):
        dr.bdecode(b"i1eX")


def _fake_api(items, inclusive_after):
    calls = []

    def fetch(endpoint, params):
        calls.append(params)
        lo, hi, limit = params["after"], params["before"], params["limit"]
        ok = [
            it
            for it in items
            if (it["created_utc"] >= lo if inclusive_after else it["created_utc"] > lo)
            and it["created_utc"] < hi
        ]
        return ok[:limit]

    return fetch, calls


@pytest.mark.parametrize("inclusive", [True, False])
def test_fetch_window_gets_everything_once_with_timestamp_ties(inclusive):
    # several items share timestamps across page boundaries
    ts = [100, 101, 101, 102, 102, 103, 104, 104, 105, 106, 106, 107]
    items = [{"id": f"x{i}", "created_utc": t} for i, t in enumerate(ts)]
    fetch, _ = _fake_api(items, inclusive)
    got = list(dr.fetch_window(fetch, "posts/search", "stocks", 100, 108, limit=4))
    assert [g["id"] for g in got] == [it["id"] for it in items]


def test_fetch_window_respects_bounds():
    items = [{"id": str(t), "created_utc": t} for t in range(90, 120)]
    fetch, _ = _fake_api(items, True)
    got = list(dr.fetch_window(fetch, "posts/search", "stocks", 100, 110, limit=4))
    assert [g["created_utc"] for g in got] == list(range(100, 110))


def test_stats_per_year(tmp_path, capsys):
    zstandard = pytest.importorskip("zstandard")
    p = tmp_path / "x_comments.zst"
    lines = [
        json.dumps({"id": "a", "created_utc": 1577836800}),  # 2020
        json.dumps({"id": "b", "created_utc": 1609459200}),  # 2021
        json.dumps({"id": "c", "created_utc": "1609459300"}),  # 2021, string ts
    ]
    p.write_bytes(zstandard.ZstdCompressor().compress(("\n".join(lines) + "\n").encode()))
    dr.main(["stats", str(p)])
    out = capsys.readouterr().out
    assert "2020" in out and "2021" in out
    assert "           2" in out
