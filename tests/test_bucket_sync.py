import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import bucket_sync as bs  # noqa: E402


class FakeBucket:
    """In-memory stand-in for the S3 calls (keys -> bytes)."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def list(self, prefix):
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)}

    def upload(self, path, key, callback):
        data = Path(path).read_bytes()
        self.objects[key] = data
        self.uploads.append(key)
        callback(len(data))

    def download(self, key, path, callback):
        Path(path).write_bytes(self.objects[key])
        callback(len(self.objects[key]))


def _tree(root: Path) -> Path:
    files = {
        "ref/stoplist.txt": b"VC\nIP\n",
        "market/eodhd/eod/AAPL.parquet": b"x" * 300,
        "market/eodhd/eod/RSH.parquet": b"y" * 50,
        "corpus/parquet/posts/stocks.parquet": b"z" * 1000,
        "raw/reddit/reddit/subreddits24/stocks_submissions.zst": b"r" * 2000,
        "raw/reddit/.work/stocks_comments/partial.zst": b"skip",
        "logs/jobs.jsonl": b"skip",
        "features/.DS_Store": b"skip",
        "features/mentions.parquet.part": b"skip",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def test_local_walk_skips_scratch_logs_and_partials(tmp_path):
    files = bs.local_files(_tree(tmp_path / "data"))
    assert set(files) == {"ref/stoplist.txt", "market/eodhd/eod/AAPL.parquet",
                          "market/eodhd/eod/RSH.parquet", "corpus/parquet/posts/stocks.parquet",
                          "raw/reddit/reddit/subreddits24/stocks_submissions.zst"}
    assert set(bs.local_files(tmp_path / "data", only="market/eodhd")) == {
        "market/eodhd/eod/AAPL.parquet", "market/eodhd/eod/RSH.parquet"}


def test_groups_diff_and_table():
    assert bs.group_of("market/eodhd/eod/A.parquet") == "market/eodhd"
    assert bs.group_of("raw/reddit/x/y.zst") == "raw/reddit"
    assert bs.group_of("ref/stoplist.txt") == "ref"
    src = {"a/1": 10, "a/2": 20, "b/3": 30}
    dst = {"a/1": 10, "a/2": 19}
    assert bs.diff(src, dst) == ["a/2", "b/3"]       # smallest first
    t = bs.table(src, dst, "local", "bucket")
    rows = {line.split()[0]: line.split() for line in t.splitlines()[2:] if line[0] != "-"}
    assert rows["a"][-2:] == ["1", "MISSING"] and rows["b"][-2:] == ["1", "MISSING"]
    assert rows["TOTAL"][1] == "3" and rows["TOTAL"][-2] == "2"
    assert "OK" in bs.table({"a/1": 1}, {"a/1": 1}, "l", "b").splitlines()[-1]


def test_push_uploads_resumes_and_verifies(tmp_path):
    data = _tree(tmp_path / "data")
    b = FakeBucket()
    out = io.StringIO()
    assert bs.push(data, b, workers=4, out=out) == 0
    assert "all files present" in out.getvalue()
    assert len(b.uploads) == 5 and all(k.startswith("data/") for k in b.uploads)
    # rerun: nothing to send; a changed file is sent again
    b.uploads.clear()
    assert bs.push(data, b, workers=4, out=io.StringIO()) == 0 and b.uploads == []
    (data / "ref/stoplist.txt").write_bytes(b"VC\nIP\nCD\n")
    assert bs.push(data, b, workers=4, out=io.StringIO()) == 0
    assert b.uploads == ["data/ref/stoplist.txt"]


def test_dry_run_sends_nothing(tmp_path):
    b = FakeBucket()
    assert bs.push(_tree(tmp_path / "data"), b, dry_run=True, out=io.StringIO()) == 0
    assert b.objects == {}


def test_pull_writes_through_part_files_and_resumes(tmp_path):
    src = _tree(tmp_path / "mac")
    b = FakeBucket()
    bs.push(src, b, workers=2, out=io.StringIO())
    dest = tmp_path / "worker"
    (dest / "ref").mkdir(parents=True)
    (dest / "ref/stoplist.txt").write_bytes(b"V")          # a truncated file from a dead upload
    assert bs.pull(dest, b, workers=2, out=io.StringIO()) == 0
    for rel in bs.local_files(src):
        assert (dest / rel).read_bytes() == (src / rel).read_bytes()
    assert not list(dest.rglob("*.part"))


def test_verify_flags_missing_and_truncated(tmp_path):
    data = _tree(tmp_path / "data")
    b = FakeBucket()
    bs.push(data, b, out=io.StringIO())
    b.objects["data/ref/stoplist.txt"] = b"V"
    del b.objects["data/market/eodhd/eod/RSH.parquet"]
    out = io.StringIO()
    assert bs.verify(data, b, "push", out=out) == 1
    assert "2 files missing or different" in out.getvalue()


def test_failed_uploads_are_reported_and_fail_the_run(tmp_path):
    data = _tree(tmp_path / "data")

    class Flaky(FakeBucket):
        def upload(self, path, key, callback):
            if key.endswith("RSH.parquet"):
                raise ConnectionError("reset by peer")
            super().upload(path, key, callback)

    b = Flaky()
    out = io.StringIO()
    err = io.StringIO()
    todo = bs.diff(bs.local_files(data), {})
    sizes = bs.local_files(data)
    fails = bs.copy(todo, sizes, lambda rel, cb: b.upload(data / rel, "data/" + rel, cb), 2,
                    out=err)
    assert fails == 1 and "FAILED market/eodhd/eod/RSH.parquet: ConnectionError" in err.getvalue()
    assert bs.push(data, b, out=out) == 1          # verify still finds it missing


def test_progress_line_counts_to_the_total():
    t = [0.0]
    out = io.StringIO()
    p = bs.Progress(total_bytes=4 * 2**20, total_files=2, out=out, clock=lambda: t[0])
    for _ in range(4):
        t[0] += 1
        p.add(2**20)
    p.file_done()
    p.file_done()
    p.finish()
    last = [line for line in out.getvalue().splitlines() if "%" in line][-1]
    assert "100.0%" in last and "2/2 files" in last and "4.0 MB / 4.0 MB" in last
    assert "1.0 MB/s" in last


def test_credentials_from_env_file_and_never_printed(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("EODHD_API_KEY=abc\nS3_ENDPOINT=https://t3.storageapi.dev\n"
                        "S3_BUCKET=data-x\nS3_ACCESS_KEY_ID=AKID123\n"
                        "export S3_SECRET_ACCESS_KEY='sekret'\n")
    creds = bs.load_creds(env={}, env_path=env_file)
    assert creds["S3_SECRET_ACCESS_KEY"] == "sekret" and "EODHD_API_KEY" not in creds
    with pytest.raises(SystemExit) as e:
        bs.load_creds(env={"S3_ENDPOINT": "x"}, env_path=tmp_path / "none")
    assert "S3_BUCKET" in str(e.value) and "sekret" not in str(e.value)
    assert "sekret" not in capsys.readouterr().out


def test_interleave_alternates_largest_and_smallest():
    assert bs.interleave(["a", "b", "c", "d", "e"]) == ["e", "a", "d", "b", "c"]
    assert bs.interleave([]) == [] and bs.interleave(["x"]) == ["x"]


def test_table_shows_folders_that_exist_only_on_the_other_side():
    t = bs.table({"a/1": 1}, {"a/1": 1, "b/2": 5}, "local", "bucket")
    row = [line.split() for line in t.splitlines() if line.startswith("b ")][0]
    assert row[1:5] == ["0", "0", "B", "1"] and row[-1] == "OK"   # visible, nothing to copy


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("cols", [40, 60, 80, 200])
def test_terminal_line_never_wraps_and_clears_the_row(cols):
    t = [0.0]
    out = FakeTTY()
    p = bs.Progress(total_bytes=6 * 2**30, total_files=71_638, out=out, clock=lambda: t[0],
                    width=lambda: cols)
    for _ in range(20):
        t[0] += 1
        p.add(3 * 2**20)
        p.file_done()
    draws = [d for d in out.getvalue().split("\r\x1b[2K") if d]
    assert draws, "nothing drawn"
    for d in draws:
        assert len(d.rstrip("\n").split("\n")[0]) <= cols - 1
        assert "%" in d and "ETA" in d
    if cols >= 200:
        assert "[" in draws[-1] and "files" in draws[-1]
