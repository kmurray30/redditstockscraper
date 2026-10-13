#!/usr/bin/env python3
"""Download historical Reddit data for selected finance subreddits.

Two sources:

* ``torrent`` - Watchful1's per-subreddit Pushshift dumps on Academic Torrents
  (2005-06 .. 2024-12). Each subreddit is one ``<sub>_submissions.zst`` and one
  ``<sub>_comments.zst`` file covering its full history. Downloads only the
  files you select, via aria2c.
* ``arctic`` - Arctic Shift's search API, for date ranges the torrent does not
  cover (2025+). Writes monthly ``.zst`` NDJSON files in the same format.

Plus two helpers:

* ``list``  - read the .torrent metadata and print exact file sizes for the
  chosen subreddits *without downloading any data*.
* ``stats`` - scan downloaded .zst files and break records/bytes down by year,
  so you can see how much of each file a given year range accounts for.

Data-use note: neither source is licensed by Reddit. Keep the data private, do
not redistribute it, and use it for inference/research only (no model
training). See docs/DATA_POLICY.md once it exists.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

TORRENT_URL = (
    "https://academictorrents.com/download/1614740ac8c94505e4ecb9d88be8bed7b6afddd4.torrent"
)
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
USER_AGENT = "redditstockscraper-research/0.1 (personal, non-commercial)"

DEFAULT_SUBS = [
    "stocks",
    "investing",
    "wallstreetbets",
    "SecurityAnalysis",
    "ValueInvesting",
    "StockMarket",
]
KINDS = ("submissions", "comments")


# --------------------------------------------------------------------------- #
# Bencode (torrent metadata) decoding
# --------------------------------------------------------------------------- #


def bdecode(data: bytes):
    """Decode a bencoded byte string. Dict keys and strings stay as bytes."""

    def parse(i: int):
        c = data[i : i + 1]
        if c == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1 : end]), end + 1
        if c == b"l":
            i += 1
            out = []
            while data[i : i + 1] != b"e":
                v, i = parse(i)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i : i + 1] != b"e":
                k, i = parse(i)
                v, i = parse(i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            n = int(data[i:colon])
            start = colon + 1
            return data[start : start + n], start + n
        raise ValueError(f"invalid bencode at offset {i}")

    value, end = parse(0)
    if end != len(data):
        raise ValueError("trailing data after bencode value")
    return value


def torrent_files(torrent_bytes: bytes) -> list[tuple[int, str, int]]:
    """Return ``(aria2_index, path, size)`` for each file; index is 1-based."""
    info = bdecode(torrent_bytes)[b"info"]
    if b"files" not in info:
        return [(1, info[b"name"].decode(), info[b"length"])]
    files = []
    for idx, f in enumerate(info[b"files"], start=1):
        path = "/".join(p.decode("utf-8", "replace") for p in f[b"path"])
        files.append((idx, path, f[b"length"]))
    return files


def load_torrent(src: str) -> bytes:
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    return Path(src).read_bytes()


def select_files(
    files: list[tuple[int, str, int]], subs: list[str], kinds: list[str]
) -> tuple[list[tuple[int, str, int]], list[str]]:
    """Match ``<sub>_<kind>.zst`` basenames case-insensitively."""
    by_name = {Path(p).name.lower(): (i, p, s) for i, p, s in files}
    chosen, missing = [], []
    for sub in subs:
        for kind in kinds:
            name = f"{sub}_{kind}.zst".lower()
            if name in by_name:
                chosen.append(by_name[name])
            else:
                missing.append(name)
    return chosen, missing


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_list(args) -> int:
    files = torrent_files(load_torrent(args.torrent))
    chosen, missing = select_files(files, args.subs, args.kinds)
    total = 0
    print(f"{'file':45} {'compressed':>12}")
    for _, path, size in sorted(chosen, key=lambda x: -x[2]):
        print(f"{Path(path).name:45} {human(size):>12}")
        total += size
    print(f"{'TOTAL':45} {human(total):>12}")
    print(
        "\nRough on-disk needs: compressed as above; fully decompressed NDJSON is"
        " typically ~7-10x larger (you never need to decompress to disk - the"
        " ingest step streams)."
    )
    if missing:
        print("\nNot found in torrent:", ", ".join(missing), file=sys.stderr)
    return 0


def cmd_torrent(args) -> int:
    aria2 = shutil.which("aria2c")
    if not aria2:
        print(
            "aria2c not found. Install it (macOS: `brew install aria2`, "
            "Debian/Ubuntu: `sudo apt install aria2`) or use any torrent client "
            "that lets you pick individual files.",
            file=sys.stderr,
        )
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    torrent_path = out / "subreddits_2024-12.torrent"
    if args.torrent.startswith(("http://", "https://")):
        if not torrent_path.exists():
            print(f"fetching {args.torrent}")
            torrent_path.write_bytes(load_torrent(args.torrent))
    else:
        torrent_path = Path(args.torrent)

    files = torrent_files(torrent_path.read_bytes())
    chosen, missing = select_files(files, args.subs, args.kinds)
    if missing:
        print("Not found in torrent:", ", ".join(missing), file=sys.stderr)
    if not chosen:
        return 1
    total = sum(s for _, _, s in chosen)
    print(f"downloading {len(chosen)} files, {human(total)} total, into {out}")
    for _, path, size in chosen:
        print(f"  {Path(path).name:45} {human(size):>12}")
    if args.dry_run:
        return 0

    indices = ",".join(str(i) for i, _, _ in chosen)
    cmd = [
        aria2,
        f"--select-file={indices}",
        "--seed-time=0",  # flip to e.g. 60 (minutes) if you want to give back
        "--file-allocation=none",
        "--continue=true",
        "--bt-remove-unselected-file=true",
        f"--dir={out}",
        str(torrent_path),
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


def _month_windows(after: datetime, before: datetime):
    cur = after.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cur < before:
        nxt = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(
            month=cur.month + 1
        )
        yield max(cur, after), min(nxt, before)
        cur = nxt


def _arctic_get(endpoint: str, params: dict, polite_delay: float) -> list[dict]:
    url = f"{ARCTIC_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    for attempt in range(8):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = json.load(r)
            time.sleep(polite_delay)
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            return payload.get("data") or []
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = float(e.headers.get("X-RateLimit-Reset") or 30)
                print(f"    rate limited; sleeping {wait:.0f}s")
                time.sleep(wait + 1)
                continue
            if e.code >= 500:
                time.sleep(2**attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            print(f"    {e!r}; retrying")
            time.sleep(2**attempt)
    raise RuntimeError(f"giving up on {url}")


def fetch_window(fetch, endpoint: str, sub: str, start: int, end: int, limit: int):
    """Yield every item in [start, end) from a created_utc-ascending search API.

    Pages by the last-seen timestamp: re-queries from one second before it and
    de-duplicates by id, so it works whether the API's ``after`` is inclusive
    or exclusive. Only fails if more than ``limit`` items share ~2 seconds, in
    which case it warns and skips ahead one second.
    """
    cursor = start
    seen_at_cursor: set[str] = set()
    while cursor < end:
        params = {
            "subreddit": sub,
            "after": cursor - 1,
            "before": end,
            "sort": "asc",
            "limit": limit,
        }
        page = fetch(endpoint, params)
        fresh = [
            it
            for it in page
            if cursor <= int(it["created_utc"]) < end and it["id"] not in seen_at_cursor
        ]
        if not fresh:
            if len(page) < limit:
                return
            print(f"    warning: >{limit} items near ts={cursor}; some may be skipped")
            cursor, seen_at_cursor = cursor + 1, set()
            continue
        yield from fresh
        last_ts = int(fresh[-1]["created_utc"])
        if last_ts != cursor:
            seen_at_cursor = set()
            cursor = last_ts
        seen_at_cursor.update(it["id"] for it in fresh if int(it["created_utc"]) == cursor)
        if len(page) < limit:
            return


def cmd_arctic(args) -> int:
    import zstandard  # only needed for this command

    after = datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc)
    before = datetime.fromisoformat(args.before).replace(tzinfo=timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def fetch(endpoint, params):
        return _arctic_get(endpoint, params, args.delay)

    for sub in args.subs:
        for kind in args.kinds:
            endpoint = "posts/search" if kind == "submissions" else "comments/search"
            for lo, hi in _month_windows(after, before):
                final = out / f"{sub}_{kind}_{lo:%Y-%m}.zst"
                if final.exists():
                    print(f"skip {final.name} (exists)")
                    continue
                tmp = final.with_suffix(".zst.part")
                n = 0
                cctx = zstandard.ZstdCompressor(level=10)
                with open(tmp, "wb") as fh, cctx.stream_writer(fh) as zw:
                    text = io.TextIOWrapper(zw, encoding="utf-8")
                    for item in fetch_window(
                        fetch, endpoint, sub, int(lo.timestamp()), int(hi.timestamp()), 100
                    ):
                        text.write(json.dumps(item, separators=(",", ":")) + "\n")
                        n += 1
                    text.flush()
                    text.detach()
                tmp.rename(final)
                print(f"{final.name}: {n:,} records, {human(final.stat().st_size)}")
    return 0


def iter_zst_lines(path: Path):
    import zstandard

    dctx = zstandard.ZstdDecompressor(max_window_size=2**31)
    with open(path, "rb") as fh, dctx.stream_reader(fh) as reader:
        for line in io.TextIOWrapper(reader, encoding="utf-8", errors="replace"):
            yield line


def cmd_stats(args) -> int:
    for p in map(Path, args.files):
        counts: dict[int, int] = defaultdict(int)
        nbytes: dict[int, int] = defaultdict(int)
        total_bytes = 0
        for line in iter_zst_lines(p):
            try:
                year = datetime.fromtimestamp(
                    int(json.loads(line)["created_utc"]), tz=timezone.utc
                ).year
            except (ValueError, KeyError):
                continue
            counts[year] += 1
            nbytes[year] += len(line)
            total_bytes += len(line)
        comp = p.stat().st_size
        print(f"\n{p.name}  (compressed {human(comp)}, raw {human(total_bytes)})")
        print(f"  {'year':>4} {'records':>12} {'share':>7} {'~compressed':>12}")
        for y in sorted(counts):
            share = nbytes[y] / total_bytes if total_bytes else 0
            print(f"  {y:>4} {counts[y]:>12,} {share:>6.1%} {human(comp * share):>12}")
    return 0


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)

    def common(p, torrent=False):
        p.add_argument("--subs", nargs="+", default=DEFAULT_SUBS)
        p.add_argument("--kinds", nargs="+", choices=KINDS, default=list(KINDS))
        if torrent:
            p.add_argument("--torrent", default=TORRENT_URL, help=".torrent path or URL")

    p = sp.add_parser("list", help="print exact sizes from torrent metadata")
    common(p, torrent=True)
    p.set_defaults(func=cmd_list)

    p = sp.add_parser("torrent", help="download selected subreddit files via aria2c")
    common(p, torrent=True)
    p.add_argument("--out", default="data/raw/reddit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_torrent)

    p = sp.add_parser("arctic", help="fetch a date range from the Arctic Shift API")
    common(p)
    p.add_argument("--after", default="2025-01-01", help="ISO date, inclusive")
    p.add_argument("--before", required=True, help="ISO date, exclusive")
    p.add_argument("--out", default="data/raw/reddit_arctic")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.set_defaults(func=cmd_arctic)

    p = sp.add_parser("stats", help="per-year record/byte breakdown of .zst files")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
