"""Copy ``data/`` between a machine and the private Railway bucket, with progress and resume.

    python scripts/bucket_sync.py status          # table: what is local, what is in the bucket
    python scripts/bucket_sync.py push            # this machine -> bucket (the Mac, once)
    python scripts/bucket_sync.py pull            # bucket -> this machine (the worker: JOB=pull-data)
    python scripts/bucket_sync.py verify          # compare sizes file by file (--direction pull
                                                  # on the worker)
    ... push --only market/eodhd                  # one folder
    ... push --dry-run                            # show what would be sent

Files land in the bucket under ``data/<path>``. A file already on the other side with the same
size is skipped, so an interrupted run resumes when it is rerun. ``pull`` writes each file to
``<file>.part`` and renames it when complete, so a killed pull never leaves a half file that
looks finished. Every push and pull ends with ``verify``, which exits non-zero if any file is
missing or has a different size.

Credentials come from the environment or ``.env``: ``S3_ENDPOINT``, ``S3_BUCKET``,
``S3_ACCESS_KEY_ID``, ``S3_SECRET_ACCESS_KEY``, optional ``S3_REGION`` (default ``auto``) and
``S3_ADDRESSING`` (``virtual``, the default, or ``path`` for older buckets). On the worker they
are Railway variable references to the bucket. They are never printed.

The Reddit data in the bucket stays private: the bucket has no public access, and nothing here
creates presigned URLs.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "data/"
CRED_NAMES = ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY")
SKIP_DIRS = ("raw/reddit/.work/", "logs/")
SKIP_NAMES = (".DS_Store",)
SKIP_SUFFIXES = (".part", ".aria2")
GROUP_DEPTH2 = ("raw", "market", "corpus")


# ------------------------------------------------------------------ listing and planning

def skipped(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (any(rel.startswith(d) for d in SKIP_DIRS) or name in SKIP_NAMES
            or name.endswith(SKIP_SUFFIXES))


def local_files(data: Path, only: str | None = None) -> dict[str, int]:
    """Relative posix path -> size for every file under ``data`` that is synced."""
    out = {}
    if not data.exists():
        return out
    for p in data.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(data).as_posix()
            if not skipped(rel) and (not only or rel.startswith(only.rstrip("/") + "/")
                                     or rel == only):
                out[rel] = p.stat().st_size
    return out


def group_of(rel: str) -> str:
    parts = rel.split("/")
    if parts[0] in GROUP_DEPTH2 and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0] if len(parts) > 1 else "(top level)"


def diff(src: dict[str, int], dst: dict[str, int]) -> list[str]:
    """Files to send: missing at the destination or with a different size."""
    return sorted((k for k, n in src.items() if dst.get(k) != n), key=lambda k: (src[k], k))


def interleave(todo: list[str]) -> list[str]:
    """Alternate the largest and smallest remaining files (``todo`` is sorted small to large).

    Big files keep the connection's bandwidth busy while the many small ones, which are bound
    by per-request latency, go through alongside them. Sending small files first instead made
    the early speed and ETA look many times worse than the real total time."""
    out, lo, hi = [], 0, len(todo) - 1
    while lo <= hi:
        out.append(todo[hi])
        hi -= 1
        if lo <= hi:
            out.append(todo[lo])
            lo += 1
    return out


def table(src: dict[str, int], dst: dict[str, int], src_name: str, dst_name: str) -> str:
    rows = defaultdict(lambda: [0, 0, 0, 0, 0])  # src files, src bytes, dst files, dst bytes, todo
    for k, n in src.items():
        r = rows[group_of(k)]
        r[0] += 1
        r[1] += n
        if dst.get(k) != n:
            r[4] += 1
    for k, n in dst.items():
        r = rows[group_of(k)]
        r[2] += 1
        r[3] += n
    head = ["folder", f"{src_name} files", f"{src_name} size", f"{dst_name} files",
            f"{dst_name} size", "to copy", "state"]
    body = []
    tot = [0, 0, 0, 0, 0]
    for g in sorted(rows):
        r = rows[g]
        tot = [a + b for a, b in zip(tot, r)]
        body.append([g, f"{r[0]:,}", human(r[1]), f"{r[2]:,}", human(r[3]), f"{r[4]:,}",
                     "OK" if r[4] == 0 else "MISSING"])
    body.append(["TOTAL", f"{tot[0]:,}", human(tot[1]), f"{tot[2]:,}", human(tot[3]),
                 f"{tot[4]:,}", "OK" if tot[4] == 0 else "MISSING"])
    widths = [max(len(str(x[i])) for x in [head] + body) for i in range(len(head))]
    fmt = lambda row: "  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)  # noqa: E731
                                for i, (c, w) in enumerate(zip(row, widths)))
    lines = [fmt(head), fmt(["-" * w for w in widths])] + [fmt(r) for r in body]
    lines.insert(len(lines) - 1, lines[1])
    return "\n".join(lines)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ------------------------------------------------------------------ progress

class Progress:
    """One status line: percent, bytes, files, recent speed, ETA. Thread-safe.

    On a terminal the line is redrawn in place; in a log (the worker) a line is printed every
    ``log_every`` seconds instead."""

    def __init__(self, total_bytes: int, total_files: int, out=None, interval: float = 0.5,
                 log_every: float = 15.0, clock=time.monotonic, width=None):
        self.total_bytes, self.total_files = total_bytes, total_files
        self.done_bytes = self.done_files = self.failed = 0
        self.out = out or sys.stderr
        self.tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.interval = interval if self.tty else log_every
        self.clock = clock
        self.width = width or (lambda: shutil.get_terminal_size(fallback=(80, 24)).columns)
        self.t0 = self._last = clock()
        self._window: deque[tuple[float, int]] = deque([(self.t0, 0)])
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self.done_bytes += n
            self._maybe_draw()

    def file_done(self, ok: bool = True) -> None:
        with self._lock:
            self.done_files += 1
            self.failed += 0 if ok else 1
            self._maybe_draw()

    def speed(self) -> float:
        now = self.clock()
        self._window.append((now, self.done_bytes))
        while len(self._window) > 2 and now - self._window[0][0] > 10:
            self._window.popleft()
        t, b = self._window[0]
        return (self.done_bytes - b) / (now - t) if now > t else 0.0

    def line(self, width: int | None = None) -> str:
        """The status line, fitted to ``width`` characters when given (a terminal).

        Wider than the terminal, a line wraps and a carriage return only goes back to the start
        of its last row, so every redraw piled up after the previous one. Fields are therefore
        dropped in order of importance: the bar shrinks, then goes, then the file count."""
        pct = 100 * self.done_bytes / self.total_bytes if self.total_bytes else 100.0
        spd = self.speed()
        left = self.total_bytes - self.done_bytes
        eta = _dur(left / spd) if spd > 0 else "?"
        fail = f"  {self.failed} failed" if self.failed else ""
        core = (f"{pct:5.1f}%  {human(self.done_bytes)} / {human(self.total_bytes)}  "
                f"{human(spd)}/s  ETA {eta}{fail}")
        files = f"  {self.done_files:,}/{self.total_files:,} files"

        def bar(n: int) -> str:
            k = int(pct / 100 * n)
            return "[" + "#" * k + "-" * (n - k) + "] "

        full = bar(25) + core + files
        if width is None:
            return full
        room = max(width - 1, 10)
        for n in range(25, 9, -5):
            if len(bar(n) + core + files) <= room:
                return bar(n) + core + files
        for candidate in (core + files, core):
            if len(candidate) <= room:
                return candidate
        return core[:room]

    def _maybe_draw(self, force: bool = False) -> None:
        now = self.clock()
        if force or now - self._last >= self.interval:
            self._last = now
            if self.tty:
                self.out.write("\r\x1b[2K" + self.line(self.width()))
            else:
                self.out.write(self.line() + "\n")
            self.out.flush()

    def finish(self) -> None:
        with self._lock:
            self._maybe_draw(force=True)
            if self.tty:
                self.out.write("\n")
            self.out.write(f"done in {_dur(self.clock() - self.t0)}\n")
            self.out.flush()


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


# ------------------------------------------------------------------ the bucket

def load_creds(env: dict | None = None, env_path: Path | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    path = env_path or ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            name = name.strip().removeprefix("export ").strip()
            if name.startswith("S3_") and value.strip() and not env.get(name):
                env[name] = value.strip().strip("'\"")
    missing = [n for n in CRED_NAMES if not env.get(n)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} (environment or .env). Copy them from the "
                         "bucket's Credentials tab in the Railway dashboard; see README.")
    return {k: v for k, v in env.items() if k.startswith("S3_")}


class Bucket:
    """The few S3 calls this tool needs, so tests can pass a fake."""

    def __init__(self, creds: dict[str, str], workers: int = 32):
        try:
            import boto3
            from boto3.s3.transfer import TransferConfig
            from botocore.config import Config
        except ImportError:
            raise SystemExit("boto3 is not installed: pip install boto3")
        self.name = creds["S3_BUCKET"]
        self.client = boto3.client(
            "s3", endpoint_url=creds["S3_ENDPOINT"], region_name=creds.get("S3_REGION") or "auto",
            aws_access_key_id=creds["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=creds["S3_SECRET_ACCESS_KEY"],
            config=Config(s3={"addressing_style": creds.get("S3_ADDRESSING") or "virtual"},
                          retries={"max_attempts": 10, "mode": "standard"},
                          max_pool_connections=max(10, workers * 2)))
        self.transfer = TransferConfig(multipart_threshold=64 * 2**20,
                                       multipart_chunksize=32 * 2**20, max_concurrency=8)

    def list(self, prefix: str) -> dict[str, int]:
        out = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.name,
                                                                           Prefix=prefix):
            for obj in page.get("Contents", []):
                out[obj["Key"]] = obj["Size"]
        return out

    def upload(self, path: Path, key: str, callback) -> None:
        self.client.upload_file(str(path), self.name, key, Config=self.transfer, Callback=callback)

    def download(self, key: str, path: Path, callback) -> None:
        self.client.download_file(self.name, key, str(path), Config=self.transfer,
                                  Callback=callback)


def remote_files(bucket, only: str | None = None) -> dict[str, int]:
    prefix = PREFIX + (only.rstrip("/") + "/" if only else "")
    return {k[len(PREFIX):]: n for k, n in bucket.list(prefix).items()
            if not k.endswith("/") and not skipped(k[len(PREFIX):])}


# ------------------------------------------------------------------ copy

def copy(todo: list[str], sizes: dict[str, int], one, workers: int, out=None) -> int:
    """Run ``one(rel, callback)`` for every path with a progress line. Returns failures."""
    out = out or sys.stderr
    prog = Progress(sum(sizes[k] for k in todo), len(todo), out=out)
    failures: list[tuple[str, str]] = []
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {pool.submit(one, rel, prog.add): rel for rel in interleave(todo)}
        for f in as_completed(futs):
            err = f.exception()
            if err is not None:
                failures.append((futs[f], f"{type(err).__name__}: {err}"))
            prog.file_done(err is None)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        out.write("\ninterrupted: rerun the same command to resume (finished files are kept)\n")
        out.flush()
        os._exit(130)
    pool.shutdown()
    prog.finish()
    for rel, msg in failures[:20]:
        out.write(f"FAILED {rel}: {msg}\n")
    if len(failures) > 20:
        out.write(f"... and {len(failures) - 20} more failures\n")
    return len(failures)


def push(data: Path, bucket, only=None, workers=32, dry_run=False, out=None) -> int:
    out = out or sys.stdout
    src, dst = local_files(data, only), remote_files(bucket, only)
    print(table(src, dst, "local", "bucket"), file=out)
    todo = diff(src, dst)
    print(f"\n{len(todo):,} files ({human(sum(src[k] for k in todo))}) to upload", file=out,
          flush=True)
    if dry_run or not todo:
        return verify(data, bucket, "push", only, out) if not dry_run else 0
    fails = copy(todo, src, lambda rel, cb: bucket.upload(data / rel, PREFIX + rel, cb), workers)
    return verify(data, bucket, "push", only, out) or (1 if fails else 0)


def pull(data: Path, bucket, only=None, workers=32, dry_run=False, out=None) -> int:
    out = out or sys.stdout
    src, dst = remote_files(bucket, only), local_files(data, only)
    print(table(src, dst, "bucket", "local"), file=out)
    todo = diff(src, dst)
    print(f"\n{len(todo):,} files ({human(sum(src[k] for k in todo))}) to download", file=out,
          flush=True)
    if dry_run or not todo:
        return verify(data, bucket, "pull", only, out) if not dry_run else 0

    def one(rel, cb):
        dest = data / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        bucket.download(PREFIX + rel, part, cb)
        os.replace(part, dest)

    fails = copy(todo, src, one, workers)
    return verify(data, bucket, "pull", only, out) or (1 if fails else 0)


def verify(data: Path, bucket, direction: str, only=None, out=None) -> int:
    """0 if every file of the source side is on the other side with the same size."""
    out = out or sys.stdout
    loc, rem = local_files(data, only), remote_files(bucket, only)
    src, dst, names = (loc, rem, ("local", "bucket")) if direction == "push" else \
        (rem, loc, ("bucket", "local"))
    bad = diff(src, dst)
    print(f"\nverify ({direction}):\n" + table(src, dst, *names), file=out)
    if bad:
        for rel in bad[:10]:
            print(f"  differs: {rel} ({src[rel]:,} vs {dst.get(rel, 'missing')})", file=out)
        print(f"{len(bad):,} files missing or different: rerun to resume", file=out, flush=True)
        return 1
    print("all files present with matching sizes", file=out, flush=True)
    return 0


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "push", "pull", "verify"])
    p.add_argument("--data", default=str(ROOT / "data"))
    p.add_argument("--only", metavar="FOLDER", help="limit to one folder under data/, "
                   "e.g. market/eodhd")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--direction", choices=["push", "pull"], default="push",
                   help="for verify: push checks local files are in the bucket (the Mac), "
                   "pull checks bucket files are local (the worker)")
    a = p.parse_args(argv)
    data = Path(a.data)
    bucket = Bucket(load_creds(), a.workers)
    if a.command == "status":
        print(table(local_files(data, a.only), remote_files(bucket, a.only), "local", "bucket"))
        code = 0
    elif a.command == "verify":
        code = verify(data, bucket, a.direction, a.only)
    else:
        code = (push if a.command == "push" else pull)(data, bucket, a.only, a.workers, a.dry_run)
    sys.exit(code)


if __name__ == "__main__":
    main()
