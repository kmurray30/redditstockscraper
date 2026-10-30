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
    assert files[0] == (1, "reddit/subreddits24/AskReddit_comments.zst", 100)
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
    assert dr.main(args) == 130  # Ctrl-C: clean exit code, no traceback
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


# --------------------------------------------------------------------------- #
# network failure handling (all faked)
# --------------------------------------------------------------------------- #

import io  # noqa: E402
import urllib.error  # noqa: E402

HASH40 = "1614740ac8c94505e4ecb9d88be8bed7b6afddd4"


def _http_err(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "Err", {}, io.BytesIO(body))


class _Resp:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_load_torrent_falls_back_through_urls_and_rejects_html(monkeypatch):
    good = benc({"info": {"name": "x", "length": 1}})
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append((req.full_url, req.get_header("User-agent")))
        n = len(seen)
        if n == 1:
            raise _http_err(404)
        if n == 2:
            return _Resp(b"<html>nope</html>")  # 200 but not a torrent
        return _Resp(good)

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    url = f"https://academictorrents.com/download/{HASH40}.torrent"
    assert dr.load_torrent(url) == good
    assert seen[0][0] == url
    assert seen[1][0] == url[: -len(".torrent")]  # extension-less variant
    assert seen[2][0] == url and seen[2][1] == dr.BROWSER_UA  # browser UA retry


def test_load_torrent_total_failure_explains_workaround(monkeypatch, tmp_path):
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_err(404, b"gone")))
    monkeypatch.setattr(dr.shutil, "which", lambda name: None)  # no aria2 -> no magnet
    with pytest.raises(RuntimeError) as ei:
        dr.load_torrent(f"https://academictorrents.com/download/{HASH40}.torrent", tmp_path)
    msg = str(ei.value)
    assert "HTTP 404" in msg and "gone" in msg and "--torrent" in msg and dr.TORRENT_PAGE in msg


def test_load_torrent_magnet_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_err(404)))
    monkeypatch.setattr(dr.shutil, "which", lambda name: "/usr/bin/aria2c")
    good = benc({"info": {"name": "x", "length": 1}})
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        (tmp_path / f"{HASH40}.torrent").write_bytes(good)

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    got = dr.load_torrent(f"https://academictorrents.com/download/{HASH40}.torrent", tmp_path)
    assert got == good
    assert "--bt-metadata-only=true" in cmds[0] and f"btih:{HASH40}" in cmds[0][-1]


def test_arctic_get_sends_iso_dates_and_reports_error_body(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        raise _http_err(422, b'{"error":"bad parameter: after"}')

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        dr._arctic_get("posts/search", {"subreddit": "stocks", "after": 1614556800,
                                        "before": 1617235200, "limit": 5}, 0)
    assert "after=2021-03-01T00%3A00%3A00" in seen[0]
    assert "before=2021-04-01T00%3A00%3A00" in seen[0]
    assert "bad parameter: after" in str(ei.value) and "422" in str(ei.value)
    assert len(seen) == 1  # 4xx (other than 429) is not retried


def test_main_turns_network_errors_into_one_line(monkeypatch, capsys):
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_err(422, b"nope")))
    rc = dr.main(["preview", "--sub", "stocks", "--month", "2021-03", "--delay", "0",
                  "--tickers", "/dev/null"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("\nerror:") and "nope" in err and "Traceback" not in err


def test_check_reports_pass_and_fail(monkeypatch, tmp_path, capsys):
    tfile = tmp_path / "t.torrent"
    tfile.write_bytes(benc({"info": {"name": "x", "length": 2048}}))
    monkeypatch.setattr(dr, "_arctic_get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("422 bad")))
    monkeypatch.setattr(dr.shutil, "which", lambda name: "/usr/bin/aria2c")
    tick = tmp_path / "tick.txt"
    tick.write_text("NET\nAAPL\n")
    rc = dr.main(["check", "--torrent", str(tfile), "--tickers", str(tick),
                  "--raw-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PASS  torrent metadata" in out and "1 files" in out
    assert "FAIL  Arctic Shift API: 422 bad" in out
    assert "PASS  ticker list (SEC): 2 tickers loaded" in out


# --------------------------------------------------------------------------- #
# Arctic Shift timeouts (reported as HTTP 422 "Timeout. Maybe slow down a bit")
# --------------------------------------------------------------------------- #

TIMEOUT_BODY = b'{"data":null,"error":"Timeout. Maybe slow down a bit"}'


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dr.time, "sleep", sleeps.append)
    monkeypatch.setattr(dr, "_arctic_penalty", 0.0)
    return sleeps


def _ok_resp(rows):
    return _Resp(json.dumps({"data": rows, "error": None}).encode())


def test_arctic_timeout_422_is_retried_and_limit_shrinks(monkeypatch, no_sleep):
    urls = []

    def fake_urlopen(req, timeout=0):
        urls.append(req.full_url)
        if len(urls) <= 3:
            raise _http_err(422, TIMEOUT_BODY)
        return _ok_resp([{"id": "a", "created_utc": 5}])

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    page = dr._arctic_get("posts/search", {"subreddit": "stocks", "limit": 100}, 0)
    assert [r["id"] for r in page] == ["a"]
    limits = [int(u.split("limit=")[1].split("&")[0]) for u in urls]
    assert limits == [100, 100, 50, 25]  # halves from the 2nd consecutive timeout onward
    assert page.limit_used == 25
    assert len(no_sleep) >= 3 and all(x <= 62 for x in no_sleep)  # backed off, capped


def test_arctic_real_bad_request_is_not_retried(monkeypatch, no_sleep):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise _http_err(422, b'{"error":"Invalid value for parameter after"}')

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="Invalid value"):
        dr._arctic_get("posts/search", {"limit": 10}, 0)
    assert len(calls) == 1


def test_arctic_gives_up_with_overloaded_message(monkeypatch, no_sleep):
    monkeypatch.setattr(dr.urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(_http_err(422, TIMEOUT_BODY)))
    with pytest.raises(RuntimeError) as ei:
        dr._arctic_get("posts/search", {"limit": 100}, 0)
    assert "overloaded" in str(ei.value) and "--delay" in str(ei.value)


def test_fetch_window_does_not_stop_early_when_page_size_was_shrunk():
    items = [{"id": f"x{i}", "created_utc": 100 + i} for i in range(10)]

    def fetch(endpoint, params):
        lo, hi = params["after"], params["before"]
        rows = [it for it in items if lo < it["created_utc"] < hi][:3]  # server only gives 3
        page = dr._Page(rows)
        page.limit_used = 3  # ...because _arctic_get shrank the limit from 100 to 3
        return page

    got = list(dr.fetch_window(fetch, "posts/search", "stocks", 100, 200, limit=100))
    assert [g["id"] for g in got] == [it["id"] for it in items]


def test_preview_prints_partial_results_on_transient_failure(monkeypatch, capsys, tmp_path):
    base = 1614556800  # 2021-03-01
    calls = []

    def fake_get(endpoint, params, delay):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("Arctic Shift kept failing (overloaded)")
        page = dr._Page([{"id": "a", "created_utc": base + 5, "title": "$NET thesis",
                          "selftext": "x"}])
        page.limit_used = 1  # a full page, so the loop asks for more (and then fails)
        return page

    monkeypatch.setattr(dr, "_arctic_get", fake_get)
    t = tmp_path / "t.txt"
    t.write_text("NET\n")
    rc = dr.main(["preview", "--sub", "stocks", "--month", "2021-03", "--delay", "0",
                  "--tickers", str(t)])
    out = capsys.readouterr().out
    assert rc == 0 and "stopped early after 1 posts" in out and "kept:ticker" in out


# --------------------------------------------------------------------------- #
# resume safety for raw downloads
# --------------------------------------------------------------------------- #


def _one_file_torrent(tmp_path, size):
    t = tmp_path / "one.torrent"
    t.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": size, "path": ["subreddits24", "stocks_submissions.zst"]}]}}))
    return t, dr.torrent_files(t.read_bytes())


def test_torrent_paths_include_the_torrent_folder_name(tmp_path):
    _, files = _one_file_torrent(tmp_path, 10)
    assert files == [(1, "reddit/subreddits24/stocks_submissions.zst", 10)]


def test_interrupted_download_with_a_control_file_is_resumed_in_its_work_folder(
        tmp_path, monkeypatch, capsys):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"
    work = dr._workdir(raw_dir, "stocks", "submissions")
    part = work / "reddit" / "subreddits24" / "stocks_submissions.zst"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x" * 10)  # full length but unfinished (holes)
    (work / "reddit.aria2").write_bytes(b"progress")

    def fake_aria2(torrent_path, indices, out):
        assert out == work and (work / "reddit.aria2").exists()  # resumes, nothing wiped
        (work / "reddit.aria2").unlink()
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    final = dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir)
    assert final == raw_dir / "reddit" / "subreddits24" / "stocks_submissions.zst"
    assert dr._marker(final).exists() and not work.exists()
    assert "resuming" in capsys.readouterr().out


def test_complete_file_with_marker_is_reused(tmp_path, monkeypatch):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"
    f = raw_dir / "reddit" / "subreddits24" / "stocks_submissions.zst"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * 10)
    dr._marker(f).write_text("10")
    monkeypatch.setattr(dr, "_aria2_fetch", lambda *a: pytest.fail("should not download"))
    assert dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir) == f


def test_file_is_found_by_name_if_aria2_used_a_different_layout(tmp_path, monkeypatch):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"

    def fake_aria2(torrent_path, indices, out):
        elsewhere = out / "other" / "stocks_submissions.zst"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_bytes(b"x" * 10)
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    got = dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir)
    assert got == raw_dir / "reddit" / "subreddits24" / "stocks_submissions.zst"
    assert got.read_bytes() == b"x" * 10


def test_unfinished_download_raises_with_resume_hint(tmp_path, monkeypatch):
    torrent, files = _one_file_torrent(tmp_path, 10)
    monkeypatch.setattr(dr, "_aria2_fetch", lambda *a: 7)  # aria2c interrupted / failed
    with pytest.raises(RuntimeError, match="Rerun the same command"):
        dr._fetch_raw("stocks", "submissions", files, torrent, tmp_path / "raw")


def test_aria2_command_is_safe_for_kept_raw_files(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(dr, "_require_aria2", lambda: "/usr/bin/aria2c")
    monkeypatch.setattr(dr.subprocess, "call", lambda cmd: seen.append(cmd) or 0)
    dr._aria2_fetch(tmp_path / "t.torrent", [3], tmp_path)
    cmd = seen[0]
    assert "--bt-remove-unselected-file=true" not in cmd  # could delete other kept raw files
    assert "--check-integrity=true" not in cmd
    assert "--continue=true" in cmd and "--select-file=3" in cmd
    assert "--console-log-level=warn" in cmd


# --------------------------------------------------------------------------- #
# filter v2 integration, fragment sweep, report
# --------------------------------------------------------------------------- #


def test_pipeline_drops_empty_comments_and_keeps_discussed_links(tmp_path, monkeypatch):
    t = 1600000000
    posts = [
        {"id": "L1", "created_utc": t, "title": "Annual letter", "selftext": "", "is_self": False,
         "num_comments": 7, "domain": "example.com"},
        {"id": "L2", "created_utc": t, "title": "Other letter", "selftext": "", "is_self": False,
         "num_comments": 1},
    ]
    comments = [
        {"id": "k", "link_id": "t3_L1", "created_utc": t + 1, "body": "good read"},
        {"id": "r", "link_id": "t3_L1", "created_utc": t + 2, "body": "[removed]"},
        {"id": "d", "link_id": "t3_L1", "created_utc": t + 3, "body": "[deleted]"},
        {"id": "x", "link_id": "t3_L2", "created_utc": t + 4, "body": "meh"},
    ]
    src = tmp_path / "src"
    _write_zst(src / "stocks_submissions.zst", posts)
    _write_zst(src / "stocks_comments.zst", comments)
    sizes = [(src / n).stat().st_size for n in ("stocks_submissions.zst", "stocks_comments.zst")]
    torrent = tmp_path / "t.torrent"
    torrent.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": sizes[0], "path": ["d", "stocks_submissions.zst"]},
        {"length": sizes[1], "path": ["d", "stocks_comments.zst"]}]}}))

    def fake_aria2(tp, indices, out):
        for i, rel, _ in dr.torrent_files(tp.read_bytes()):
            if i in indices:
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_bytes((src / Path(rel).name).read_bytes())
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    tick = tmp_path / "t.txt"
    tick.write_text("NET\n")
    args = ["pipeline", "--subs", "stocks", "--torrent", str(torrent), "--tickers", str(tick),
            "--raw-dir", str(tmp_path / "raw"), "--out", str(tmp_path / "corpus")]
    assert dr.main(args) == 0
    out = tmp_path / "corpus" / "stocks"
    assert [p["id"] for p in _read_zst(out / "posts.zst")] == ["L1"]
    assert [c["id"] for c in _read_zst(out / "comments.zst")] == ["k"]
    m = json.loads((out / "manifest.json").read_text())
    assert m["posts"]["kept:discussed_link"] == 1 and m["comments"]["dropped_empty"] == 2
    assert m["filter_version"] == dr.FILTER_VERSION and m["min_link_comments"] == 5
    # changing the threshold invalidates the finished manifest, so it re-runs
    assert dr.main(args + ["--min-link-comments", "1"]) == 0
    m = json.loads((out / "manifest.json").read_text())
    assert m["min_link_comments"] == 1 and m["posts"]["kept:discussed_link"] == 2


def test_sweep_untrusted_deletes_unmarked_files_even_if_full_length(tmp_path):
    files = [(1, "r/d/a_comments.zst", 100), (2, "r/d/b_comments.zst", 100),
             (3, "r/d/c_comments.zst", 100)]
    folder = tmp_path / "r" / "d"
    folder.mkdir(parents=True)
    (folder / "a_comments.zst").write_bytes(b"\0" * 100)  # sparse spill: full length, no marker
    (folder / "b_comments.zst").write_bytes(b"x" * 100)   # verified earlier (--keep-raw)
    dr._marker(folder / "b_comments.zst").write_text("100")
    (folder / "c_comments.zst").write_bytes(b"x" * 40)    # partial spill
    (folder / "unrelated.txt").write_bytes(b"hi")
    removed, freed = dr.sweep_untrusted(files, folder)
    assert (removed, freed) == (2, 140)
    assert sorted(p.name for p in folder.iterdir()) == [
        "b_comments.zst", "b_comments.zst.ok", "unrelated.txt"]


def test_clean_raw_removes_unverified_and_refuses_while_a_download_is_in_progress(
        tmp_path, capsys):
    t = tmp_path / "t.torrent"
    t.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": 100, "path": ["d", "a.zst"]}, {"length": 100, "path": ["d", "b.zst"]}]}}))
    raw = tmp_path / "raw"
    folder = raw / "reddit" / "d"
    folder.mkdir(parents=True)
    (folder / "a.zst").write_bytes(b"x" * 100)  # full length but unverified (old spill)
    (folder / "b.zst").write_bytes(b"x" * 100)
    dr._marker(folder / "b.zst").write_text("100")
    (raw / "reddit.aria2").write_bytes(b"old-version progress file")
    work = raw / ".work" / "stocks_comments"
    work.mkdir(parents=True)
    (work / "reddit.aria2").write_bytes(b"interrupted download")
    (work / "partial.bin").write_bytes(b"x" * 7)
    args = ["clean-raw", "--torrent", str(t), "--raw-dir", str(raw)]
    assert dr.main(args) == 1
    assert "stocks_comments" in capsys.readouterr().out
    assert (folder / "a.zst").exists() and work.exists()  # nothing touched
    assert dr.main(args + ["--force"]) == 0
    assert "removed 1 unverified files" in capsys.readouterr().out
    assert sorted(p.name for p in folder.iterdir()) == ["b.zst", "b.zst.ok"]
    assert not work.exists() and not (raw / "reddit.aria2").exists()
    assert dr.main(args) == 0  # nothing left to refuse on


def test_report_command(tmp_path, capsys):
    out = tmp_path / "stocks"
    t18, t19 = 1514764800, 1546300800  # 2018-01-01, 2019-01-01
    _write_zst(out / "posts.zst", [
        {"id": "a", "created_utc": t18, "title": "Thesis on NET", "matched_tickers": ["NET"]},
        {"id": "b", "created_utc": t19, "title": "A letter", "matched_tickers": []}])
    _write_zst(out / "comments.zst", [{"id": "c%d" % i, "created_utc": t18 + i} for i in range(4)])
    (out / "mentions_posts.csv").write_text(
        "date,subreddit,ticker,source,count\n2018-01-01,stocks,NET,post,3\n")
    (out / "mentions_comments.csv").write_text(
        "date,subreddit,ticker,source,count\n2018-01-01,stocks,NET,comment,4\n"
        "2018-01-02,stocks,IT,comment,2\n")
    (out / "manifest.json").write_text(json.dumps({
        "filter_version": "posts-v2", "years": [2005, 2024],
        "posts": {"kept:ticker": 1, "kept:discussed_link": 1, "dropped:no_text": 2},
        "comments": {"kept": 4, "dropped": 6, "dropped_empty": 2}}))
    assert dr.main(["report", "--sub", "stocks", "--out", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "posts: kept 2 of 4 (50.0%)" in text
    assert "NET:7" in text and "IT:2" in text
    assert "[2018 NET] Thesis on NET" in text
    assert "2018:      1 posts        4 comments" in text
    assert dr.main(["report", "--sub", "nope", "--out", str(tmp_path)]) == 1


# --------------------------------------------------------------------------- #
# stale aria2c control file ("OK, 0B/s" but the file is gone)
# --------------------------------------------------------------------------- #


def test_second_fetch_in_the_same_torrent_folder_works(tmp_path, monkeypatch):
    """Regression: aria2c errors out (code 13) in a torrent folder that exists without its
    progress file. Every fetch gets a fresh scratch folder, so a second one can't hit that."""
    t = tmp_path / "t.torrent"
    t.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": 10, "path": ["d", "a_submissions.zst"]},
        {"length": 12, "path": ["d", "a_comments.zst"]}]}}))
    files = dr.torrent_files(t.read_bytes())
    raw_dir = tmp_path / "raw"
    sizes = {1: 10, 2: 12}

    def fake_aria2(torrent_path, indices, out):
        top = out / "reddit"
        if top.exists() and not (out / "reddit.aria2").exists():
            return 13  # what real aria2c does with an existing folder and no progress file
        (top / "d").mkdir(parents=True, exist_ok=True)
        i = indices[0]
        name = "a_submissions.zst" if i == 1 else "a_comments.zst"
        (top / "d" / name).write_bytes(b"x" * sizes[i])
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    first = dr._fetch_raw("a", "submissions", files, t, raw_dir)
    dr._discard(first, False, raw_dir)  # as the pipeline does after filtering
    second = dr._fetch_raw("a", "comments", files, t, raw_dir)
    assert second.read_bytes() == b"x" * 12 and dr._marker(second).exists()


def test_neighbour_spill_never_reaches_the_raw_folder(tmp_path, monkeypatch):
    t = tmp_path / "t.torrent"
    t.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": 50, "path": ["d", "x_comments.zst"]},
        {"length": 10, "path": ["d", "x_submissions.zst"]}]}}))
    files = dr.torrent_files(t.read_bytes())
    raw_dir = tmp_path / "raw"

    def fake_aria2(torrent_path, indices, out):
        folder = out / "reddit" / "d"
        folder.mkdir(parents=True)
        (folder / "x_submissions.zst").write_bytes(b"s" * 10)
        (folder / "x_comments.zst").write_bytes(b"\0" * 50)  # neighbour spill, full length
        (out / "reddit.aria2").write_bytes(b"ctl")
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    got = dr._fetch_raw("x", "submissions", files, t, raw_dir)
    assert got.read_bytes() == b"s" * 10 and dr._marker(got).exists()
    left = sorted(p.name for p in (raw_dir / "reddit" / "d").iterdir())
    assert left == ["x_submissions.zst", "x_submissions.zst.ok"]
    assert not (raw_dir / ".work" / "x_submissions").exists()


def test_leftovers_without_a_control_file_are_wiped_before_downloading(tmp_path, monkeypatch):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"
    work = dr._workdir(raw_dir, "stocks", "submissions")
    junk = work / "reddit" / "subreddits24" / "stocks_submissions.zst"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"\0" * 10)  # full length, mostly holes, nothing to resume from

    def fake_aria2(torrent_path, indices, out):
        assert not junk.exists()
        p = out / "reddit" / "subreddits24" / "stocks_submissions.zst"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x" * 10)
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    got = dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir)
    assert got.read_bytes() == b"x" * 10


def test_aria2_success_without_the_file_starts_that_fetch_over(tmp_path, monkeypatch, capsys):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"
    calls = []

    def fake_aria2(torrent_path, indices, out):
        calls.append(sorted(p.name for p in out.rglob("*")))
        if len(calls) == 1:
            return 0  # "OK, 0B/s" but wrote nothing
        p = out / "reddit" / "subreddits24" / "stocks_submissions.zst"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x" * 10)
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    got = dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir)
    assert got.read_bytes() == b"x" * 10 and len(calls) == 2
    assert "starting this file over" in capsys.readouterr().out


def test_a_trusted_file_from_an_earlier_run_is_not_touched_by_a_later_fetch(
        tmp_path, monkeypatch):
    t = tmp_path / "t.torrent"
    t.write_bytes(benc({"info": {"name": "reddit", "piece length": 1, "pieces": b"", "files": [
        {"length": 30, "path": ["d", "y_comments.zst"]},
        {"length": 10, "path": ["d", "x_submissions.zst"]}]}}))
    files = dr.torrent_files(t.read_bytes())
    raw_dir = tmp_path / "raw"
    kept = raw_dir / "reddit" / "d" / "y_comments.zst"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"k" * 30)
    dr._marker(kept).write_text("30")

    def fake_aria2(torrent_path, indices, out):
        (out / "reddit" / "d").mkdir(parents=True)
        (out / "reddit" / "d" / "x_submissions.zst").write_bytes(b"s" * 10)
        (out / "reddit" / "d" / "y_comments.zst").write_bytes(b"\0" * 30)  # spill in scratch
        return 0

    monkeypatch.setattr(dr, "_aria2_fetch", fake_aria2)
    dr._fetch_raw("x", "submissions", files, t, raw_dir)
    assert kept.read_bytes() == b"k" * 30 and dr._marker(kept).exists()


def test_discard_removes_file_and_marker_unless_keep_raw(tmp_path):
    raw_dir = tmp_path / "raw"
    f = raw_dir / "reddit" / "subreddits24" / "a.zst"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    dr._marker(f).write_text("1")
    dr._discard(f, keep_raw=True, raw_dir=raw_dir)
    assert f.exists() and dr._marker(f).exists()
    dr._discard(f, keep_raw=False, raw_dir=raw_dir)
    assert not f.exists() and not dr._marker(f).exists()


def test_persistent_failure_reports_sizes_and_keeps_the_work_folder_for_resume(
        tmp_path, monkeypatch):
    torrent, files = _one_file_torrent(tmp_path, 10)
    raw_dir = tmp_path / "raw"
    monkeypatch.setattr(dr, "_aria2_fetch", lambda *a, **k: 0)  # never writes the file
    with pytest.raises(RuntimeError) as ei:
        dr._fetch_raw("stocks", "submissions", files, torrent, raw_dir)
    msg = str(ei.value)
    assert "expected size: 10" in msg and "on disk: missing" in msg
    assert "Torrent not registered" in msg


def test_aria2_prints_what_is_normal_before_starting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dr, "_require_aria2", lambda: "/usr/bin/aria2c")
    monkeypatch.setattr(dr.subprocess, "call", lambda cmd: 0)
    dr._aria2_fetch(tmp_path / "t.torrent", [1], tmp_path)
    out = capsys.readouterr().out
    assert "Torrent not registered" in out and "Checksum error" in out and "Ctrl-C is safe" in out


def test_ctrl_c_exits_cleanly_without_a_traceback(monkeypatch, capsys):
    def boom(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(dr, "cmd_stats", boom)
    rc = dr.main(["stats", "whatever.zst"])
    err = capsys.readouterr().err
    assert rc == 130 and "rerun the same command to resume" in err and "Traceback" not in err


def test_changing_the_stoplist_reruns_a_finished_subreddit(pipeline_env, tmp_path):
    tmp, args, fetched = pipeline_env
    assert dr.main(args + ["--keep-raw"]) == 0
    fetched.clear()
    assert dr.main(args) == 0 and fetched == []  # unchanged settings: skipped
    stop = tmp / "stop.txt"
    stop.write_text("NET\n")
    assert dr.main(args + ["--stoplist", str(stop)]) == 0
    out = tmp / "corpus" / "stocks"
    m = json.loads((out / "manifest.json").read_text())
    assert m["stoplist"] != ""
    # it re-ran with the new setting; the post survives because "$NET" is a cashtag, and
    # cashtags still count for stoplisted words
    assert [p["id"] for p in _read_zst(out / "posts.zst")] == ["a"]


def test_report_top_option_limits_the_ticker_list(tmp_path, capsys):
    out = tmp_path / "s"
    _write_zst(out / "posts.zst", [])
    (out / "mentions_posts.csv").write_text(
        "date,subreddit,ticker,source,count\n" + "".join(
            f"2018-01-01,s,T{i},post,{100 - i}\n" for i in range(10)))
    (out / "manifest.json").write_text(json.dumps({
        "filter_version": "posts-v4", "years": [2005, 2024], "posts": {"kept:ticker": 1},
        "comments": {"kept": 1}}))
    assert dr.main(["report", "--sub", "s", "--out", str(tmp_path), "--top", "3"]) == 0
    text = capsys.readouterr().out
    assert "top 3 tickers" in text and "T2:98" in text and "T3:" not in text


def _make_corpus_sub(root, name, titles, comments, mentions):
    out = root / name
    t = 1514764800  # 2018-01-01
    _write_zst(out / "posts.zst", [{"id": f"p{i}", "created_utc": t + i, "title": ttl,
                                    "selftext": "", "matched_tickers": []}
                                   for i, ttl in enumerate(titles)])
    _write_zst(out / "comments.zst", [{"id": f"c{i}", "created_utc": t + i, "body": b}
                                      for i, b in enumerate(comments)])
    (out / "mentions_comments.csv").write_text(
        "date,subreddit,ticker,source,count\n" + "".join(
            f"2018-01-01,{name},{tk},comment,{n}\n" for tk, n in mentions.items()))
    (out / "manifest.json").write_text(json.dumps({
        "filter_version": "x", "years": [2005, 2024], "posts": {"kept:ticker": len(titles)},
        "comments": {"kept": len(comments), "dropped": 1}}))


def test_report_all_flags_ordinary_words_and_writes_a_stoplist(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dr, "WORD_MIN_USES", 5)
    root = tmp_path / "corpus"
    # "lot" is mostly written in lowercase, AAPL always in capitals
    _make_corpus_sub(root, "alpha", ["AAPL up", "a lot of risk"],
                     ["a lot", "such a lot", "Lot of it", "a lot", "one LOT", "AAPL AAPL AAPL"],
                     {"LOT": 50, "AAPL": 40})
    _make_corpus_sub(root, "beta", ["Is AAPL cheap?"], ["AAPL yes"], {"AAPL": 5})
    stop = tmp_path / "stop.txt"
    rc = dr.main(["report", "--all", "--out", str(root), "--top", "5",
                  "--write-stoplist", str(stop)])
    text = capsys.readouterr().out
    assert rc == 0
    assert "== alpha" in text and "== beta" in text and "SUMMARY" in text
    assert "probably ordinary words" in text and "LOT(" in text
    assert "AAPL(" not in text.split("probably ordinary words")[1].split("\n")[1]
    assert "<=2018 | 2019-20 | 2021+" in text
    assert stop.read_text().splitlines()[1:] == ["LOT"]
    # re-running merges instead of overwriting
    stop.write_text("# mine\nZZZ\n")
    dr.main(["report", "--all", "--out", str(root), "--top", "5", "--write-stoplist", str(stop)])
    assert stop.read_text().splitlines()[1:] == ["LOT", "ZZZ"]


def test_report_survives_case_folding_lookalikes(tmp_path, capsys):
    # Turkish dotted capital I: matches the case-insensitive ticker regex as "NIO" but
    # upper-cases to a string that is not a ticker. It used to crash the report.
    root = tmp_path / "corpus"
    _make_corpus_sub(root, "alpha", ["NIO up", "N\u0130O rally"], ["NIO", "N\u0130O"], {"NIO": 5})
    assert dr.main(["report", "--sub", "alpha", "--out", str(root), "--top", "5"]) == 0


def test_report_needs_sub_or_all(tmp_path, capsys):
    assert dr.main(["report", "--out", str(tmp_path)]) == 1
    assert "--sub NAME or --all" in capsys.readouterr().out


def test_manifest_records_the_ticker_list_and_a_new_list_reruns_the_subreddit(pipeline_env):
    tmp, args, fetched = pipeline_env
    assert dr.main(args + ["--keep-raw"]) == 0
    out = tmp / "corpus" / "stocks"
    m = json.loads((out / "manifest.json").read_text())
    assert m["tickers"] and m["tickers_source"].endswith("tickers.txt")
    fetched.clear()
    assert dr.main(args) == 0 and fetched == []          # same list: skipped
    bigger = tmp / "tickers2.txt"
    bigger.write_text("NET\nAAPL\nTSLA\nRSH\n")            # a delisted name added
    args2 = [a if a != str(tmp / "tickers.txt") else str(bigger) for a in args]
    assert dr.main(args2) == 0
    m2 = json.loads((out / "manifest.json").read_text())
    assert m2["tickers"] != m["tickers"] and m2["tickers_source"].endswith("tickers2.txt")


def test_arctic_retries_a_dropped_connection(monkeypatch, no_sleep):
    import http.client
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"x" * 10)
        if len(calls) == 2:
            raise ConnectionResetError("reset by peer")
        return _ok_resp([{"id": "a", "created_utc": 5}])

    monkeypatch.setattr(dr.urllib.request, "urlopen", fake_urlopen)
    page = dr._arctic_get("posts/search", {"limit": 100}, 0)
    assert [r["id"] for r in page] == ["a"] and len(calls) == 3
