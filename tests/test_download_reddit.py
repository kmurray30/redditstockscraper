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


# --------------------------------------------------------------------------- #
# pipeline (download -> filter -> delete raw), with aria2 faked
# --------------------------------------------------------------------------- #


def _write_zst(path, records):
    zstandard = pytest.importorskip("zstandard")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r) + "\n" for r in records).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(data))


def _read_zst(path):
    return [json.loads(line) for line in dr.iter_zst_lines(path)]


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    t2020 = 1600000000  # 2020-09-13
    posts = [
        {"id": "a", "created_utc": t2020, "title": "Deep dive on $NET", "selftext": "long",
         "subreddit": "stocks", "junk_field": 1},
        {"id": "b", "created_utc": t2020, "title": "Daily Discussion Thread", "selftext": "",
         "stickied": True},
        {"id": "c", "created_utc": t2020, "title": "lol", "selftext": "moon"},
        {"id": "d", "created_utc": 1300000000, "title": "$AAPL in 2011", "selftext": ""},
    ]
    comments = [
        {"id": "c1", "link_id": "t3_a", "created_utc": t2020 + 60, "body": "NET margins are great"},
        {"id": "c2", "link_id": "t3_b", "created_utc": t2020 + 60, "body": "$TSLA calls"},
        {"id": "c3", "link_id": "t3_b", "created_utc": t2020 + 90, "body": "$TSLA puts"},
        {"id": "c4", "link_id": "t3_c", "created_utc": t2020 + 60, "body": "nice"},
    ]
    src = tmp_path / "src"
    _write_zst(src / "stocks_submissions.zst", posts)
    _write_zst(src / "stocks_comments.zst", comments)
    sizes = {n: (src / n).stat().st_size for n in ("stocks_submissions.zst", "stocks_comments.zst")}
    torrent = tmp_path / "t.torrent"
    torrent.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": sizes["stocks_submissions.zst"], "path": ["subreddits24", "stocks_submissions.zst"]},
        {"length": sizes["stocks_comments.zst"], "path": ["subreddits24", "stocks_comments.zst"]},
    ]}}))
    tickers = tmp_path / "tickers.txt"
    tickers.write_text("NET\nAAPL\nTSLA\n")

    fetched = []

    def fake_aria2(torrent_path, indices, out):
        files = dr.torrent_files(torrent_path.read_bytes())
        for i, rel, _ in files:
            if i in indices:
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((src / Path(rel).name).read_bytes())
                fetched.append(Path(rel).name)
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    args = ["pipeline", "--subs", "stocks", "--torrent", str(torrent), "--tickers", str(tickers),
            "--raw-dir", str(tmp_path / "raw"), "--out", str(tmp_path / "corpus"),
            "--from-year", "2015"]
    return tmp_path, args, fetched


def test_pipeline_filters_and_deletes_raw(pipeline_env):
    tmp, args, fetched = pipeline_env
    assert dr.main(args) == 0
    out = tmp / "corpus" / "stocks"
    posts = _read_zst(out / "posts.zst")
    assert [p["id"] for p in posts] == ["a"]  # megathread, noise, pre-2015 dropped
    assert "junk_field" not in posts[0] and posts[0]["matched_tickers"] == ["NET"]
    assert [c["id"] for c in _read_zst(out / "comments.zst")] == ["c1"]
    # mentions include dropped content (the megathread's comments)
    mentions = (out / "mentions_comments.csv").read_text()
    assert "2020-09-13,stocks,TSLA,comment,2" in mentions
    assert "NET,comment,1" in mentions
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["posts"]["kept:ticker"] == 1 and manifest["posts"]["out_of_years"] == 1
    assert not list((tmp / "raw").rglob("*.zst"))
    assert fetched == ["stocks_submissions.zst", "stocks_comments.zst"]


def test_pipeline_keep_raw_and_resume_skips(pipeline_env):
    tmp, args, fetched = pipeline_env
    assert dr.main(args + ["--keep-raw"]) == 0
    assert len(list((tmp / "raw").rglob("*.zst"))) == 2
    fetched.clear()
    assert dr.main(args) == 0  # manifest matches -> nothing re-fetched
    assert fetched == []


def test_pipeline_resumes_after_posts_stage(pipeline_env, monkeypatch):
    tmp, args, fetched = pipeline_env
    real = dr.filter_comments

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(dr, "filter_comments", boom)
    with pytest.raises(KeyboardInterrupt):
        dr.main(args)
    assert not (tmp / "corpus" / "stocks" / "comments.zst").exists()  # .part cleaned up
    monkeypatch.setattr(dr, "filter_comments", real)
    fetched.clear()
    assert dr.main(args) == 0
    # posts are not re-filtered and the interrupted comments raw file is reused
    assert fetched == []
    assert not list((tmp / "raw").rglob("*.zst"))
    assert [c["id"] for c in _read_zst(tmp / "corpus" / "stocks" / "comments.zst")] == ["c1"]


def test_arctic_fetches_comments_only_for_kept_threads(tmp_path, monkeypatch):
    pytest.importorskip("zstandard")
    base = 1735689600  # 2025-01-01
    posts = [
        {"id": "k1", "created_utc": base + 100, "title": "$NET thesis", "selftext": "x"},
        {"id": "d1", "created_utc": base + 200, "title": "lol", "selftext": "moon"},
    ]
    comments = [{"id": "x1", "link_id": "t3_k1", "created_utc": base + 300, "body": "agree NET"}]
    calls = []

    def fake_get(endpoint, params, delay):
        calls.append((endpoint, dict(params)))
        if endpoint == "posts/search":
            src = posts
        else:
            src = [c for c in comments if c["link_id"] == "t3_" + params["link_id"]]
        return [r for r in src if params["after"] < r["created_utc"] < params["before"]][
            : params["limit"]]

    monkeypatch.setattr(dr, "_arctic_get", fake_get)
    tickers = tmp_path / "t.txt"
    tickers.write_text("NET\n")
    dr.main(["arctic", "--subs", "stocks", "--after", "2025-01-01", "--before", "2025-02-01",
             "--out", str(tmp_path), "--delay", "0", "--tickers", str(tickers)])
    comment_calls = [p for e, p in calls if e == "comments/search"]
    assert {p["link_id"] for p in comment_calls} == {"k1"}
    out = tmp_path / "stocks"
    assert [p["id"] for p in _read_zst(out / "posts_2025-01.zst")] == ["k1"]
    assert [c["id"] for c in _read_zst(out / "comments_2025-01.zst")] == ["x1"]
    assert (out / "mentions_2025-01.csv").exists()
