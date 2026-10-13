#!/usr/bin/env python3
"""Download historical Reddit data for selected finance subreddits.

Sources:

* Watchful1's per-subreddit Pushshift dumps on Academic Torrents
  (2005-06 .. 2024-12). Each subreddit is one ``<sub>_submissions.zst`` and one
  ``<sub>_comments.zst`` file covering its full history.
* Arctic Shift's search API, for 2025+ (which the torrent does not cover).

Commands:

* ``list``     - exact file sizes from the torrent metadata; downloads no data.
* ``pipeline`` - the main path. Per subreddit: download the submissions file,
  stream-filter it to stock-relevant posts, delete the raw file, then do the
  same for comments, keeping only comments in kept threads. Peak disk is the
  largest single raw file, never the whole corpus.
* ``preview``  - run the filter over a sample and show what it keeps/drops.
* ``arctic``   - the same filter over the Arctic Shift API, month by month;
  comments are fetched only for kept threads.
* ``torrent``  - plain download of whole files (no filtering).
* ``stats``    - per-year record/byte breakdown of .zst files.

Data-use note: neither source is licensed by Reddit. Keep the data private, do
not redistribute it, and use it for inference/research only (no model
training).
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reddit_filter import (  # noqa: E402
    COMMENT_FIELDS,
    FILTER_VERSION,
    POST_FIELDS,
    MentionCounter,
    TickerMatcher,
    is_relevant_post,
    load_tickers,
    slim,
)

try:  # ~3-5x faster JSON parsing on big comment files, if installed
    import orjson

    _loads = orjson.loads
except ImportError:  # pragma: no cover
    _loads = json.loads

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


def _require_aria2() -> str | None:
    aria2 = shutil.which("aria2c")
    if not aria2:
        print(
            "aria2c not found. Install it (macOS: `brew install aria2`, "
            "Debian/Ubuntu: `sudo apt install aria2`).",
            file=sys.stderr,
        )
    return aria2


def _ensure_torrent(src: str, out: Path) -> Path:
    if not src.startswith(("http://", "https://")):
        return Path(src)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "subreddits_2024-12.torrent"
    if not path.exists():
        print(f"fetching {src}")
        path.write_bytes(load_torrent(src))
    return path


def _aria2_fetch(torrent_path: Path, indices: list[int], out: Path) -> int:
    """Download only the given (1-based) file indices of a torrent into ``out``."""
    aria2 = _require_aria2()
    if not aria2:
        return 1
    cmd = [
        aria2,
        f"--select-file={','.join(map(str, indices))}",
        "--seed-time=0",  # flip to e.g. 60 (minutes) if you want to give back
        "--file-allocation=none",
        "--continue=true",
        "--bt-remove-unselected-file=true",
        "--summary-interval=60",
        f"--dir={out}",
        str(torrent_path),
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


def cmd_torrent(args) -> int:
    out = Path(args.out)
    torrent_path = _ensure_torrent(args.torrent, out)
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
    return _aria2_fetch(torrent_path, [i for i, _, _ in chosen], out)


# --------------------------------------------------------------------------- #
# Filtering pipeline
# --------------------------------------------------------------------------- #


class ZstLineWriter:
    """Write NDJSON records to ``path`` via ``path.part``; renamed only on success."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + ".part")
        self.n = 0

    def __enter__(self):
        import zstandard

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.tmp, "wb")
        self._zw = zstandard.ZstdCompressor(level=10, threads=-1).stream_writer(self._fh)
        return self

    def write(self, obj: dict) -> None:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"
        self._zw.write(line.encode("utf-8"))
        self.n += 1

    def __exit__(self, exc_type, exc, tb):
        self._zw.close()  # also closes the file
        if exc_type is None:
            self.tmp.replace(self.path)
        else:
            self.tmp.unlink(missing_ok=True)
        return False


def iter_records(path: Path):
    for line in iter_zst_lines(path):
        try:
            yield _loads(line)
        except ValueError:
            continue


def _year(created_utc) -> int:
    return datetime.fromtimestamp(int(float(created_utc)), tz=timezone.utc).year


def filter_posts(records, sub, matcher, counter, years, writer, stats) -> set[str]:
    """Write relevant posts; return their ids. Mentions are counted for all posts."""
    kept: set[str] = set()
    for rec in records:
        ts = rec.get("created_utc")
        if ts is None or "id" not in rec:
            stats["malformed"] += 1
            continue
        if not years[0] <= _year(ts) <= years[1]:
            stats["out_of_years"] += 1
            continue
        counter.add(
            ts, sub, matcher.match(rec.get("title")) | matcher.match(rec.get("selftext")), "post"
        )
        keep, reason, tickers = is_relevant_post(rec, matcher)
        stats[("kept:" if keep else "dropped:") + reason] += 1
        if keep:
            writer.write(slim(rec, POST_FIELDS, tickers))
            kept.add(rec["id"])
    return kept


def filter_comments(records, sub, kept_posts, matcher, counter, years, writer, stats) -> None:
    """Keep comments whose thread was kept. Mentions are counted for all comments."""
    for rec in records:
        ts = rec.get("created_utc")
        if ts is None:
            stats["malformed"] += 1
            continue
        if years[0] <= _year(ts) <= years[1]:
            counter.add(ts, sub, matcher.match(rec.get("body")), "comment")
        link = (rec.get("link_id") or "").removeprefix("t3_")
        if link in kept_posts:
            writer.write(slim(rec, COMMENT_FIELDS))
            stats["kept"] += 1
        else:
            stats["dropped"] += 1


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def _fetch_raw(sub, kind, files, torrent_path, raw_dir) -> Path | None:
    chosen, _ = select_files(files, [sub], [kind])
    if not chosen:
        print(f"  {sub}_{kind}.zst is not in the torrent; skipping")
        return None
    idx, rel, size = chosen[0]
    raw = raw_dir / rel
    if raw.exists() and raw.stat().st_size == size:
        print(f"  using existing {raw}")
        return raw
    print(f"  downloading {Path(rel).name} ({human(size)})")
    rc = _aria2_fetch(torrent_path, [idx], raw_dir)
    if rc != 0 or not raw.exists() or raw.stat().st_size != size:
        raise RuntimeError(f"download of {rel} failed (aria2c exit code {rc})")
    return raw


def _discard(raw: Path, keep_raw: bool) -> None:
    if keep_raw:
        return
    raw.unlink(missing_ok=True)
    Path(str(raw) + ".aria2").unlink(missing_ok=True)


def _print_stats(label: str, stats: Counter) -> None:
    total = sum(stats.values())
    kept = sum(v for k, v in stats.items() if k.startswith("kept"))
    print(f"  {label}: kept {kept:,} of {total:,} ({kept / total:.1%})" if total else f"  {label}: empty")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"      {k:24} {v:>12,}")


def run_sub_pipeline(sub, files, torrent_path, raw_dir, corpus_dir, matcher, years,
                     keep_raw=False, comments=True) -> dict:
    sub_dir = corpus_dir / sub
    sub_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"filter_version": FILTER_VERSION, "years": list(years)}
    manifest_path = sub_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if manifest and all(manifest.get(k) == v for k, v in stamp.items()):
        print(f"[{sub}] already done ({manifest_path}); skipping")
        return manifest

    print(f"[{sub}] posts")
    posts_out = sub_dir / "posts.zst"
    posts_stage = sub_dir / "posts.stage.json"
    stage = _read_json(posts_stage)
    if stage and all(stage.get(k) == v for k, v in stamp.items()) and posts_out.exists():
        kept = {r["id"] for r in iter_records(posts_out)}
        post_stats = Counter(stage["stats"])
        print(f"  resuming: {len(kept):,} kept posts already filtered")
    else:
        raw = _fetch_raw(sub, "submissions", files, torrent_path, raw_dir)
        if raw is None:
            return {}
        post_stats, counter = Counter(), MentionCounter()
        with ZstLineWriter(posts_out) as w:
            kept = filter_posts(iter_records(raw), sub, matcher, counter, years, w, post_stats)
        counter.write_csv(sub_dir / "mentions_posts.csv")
        _write_json(posts_stage, {**stamp, "stats": post_stats, "raw_bytes": raw.stat().st_size})
        _discard(raw, keep_raw)
    _print_stats("posts", post_stats)

    comment_stats: Counter = Counter()
    if comments:
        print(f"[{sub}] comments")
        raw = _fetch_raw(sub, "comments", files, torrent_path, raw_dir)
        if raw is not None:
            counter = MentionCounter()
            with ZstLineWriter(sub_dir / "comments.zst") as w:
                filter_comments(iter_records(raw), sub, kept, matcher, counter, years, w,
                                comment_stats)
            counter.write_csv(sub_dir / "mentions_comments.csv")
            _discard(raw, keep_raw)
        _print_stats("comments", comment_stats)

    manifest = {
        **stamp,
        "subreddit": sub,
        "source": "academictorrents subreddits 2005-06..2024-12",
        "torrent": str(torrent_path),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "posts": dict(post_stats),
        "comments": dict(comment_stats),
        "comments_processed": comments,
        "output_bytes": {
            p.name: p.stat().st_size for p in sub_dir.iterdir() if p.suffix in (".zst", ".csv")
        },
    }
    _write_json(manifest_path, manifest)
    posts_stage.unlink(missing_ok=True)
    return manifest


def cmd_pipeline(args) -> int:
    raw_dir, corpus_dir = Path(args.raw_dir), Path(args.out)
    torrent_path = _ensure_torrent(args.torrent, raw_dir)
    files = torrent_files(torrent_path.read_bytes())
    matcher = TickerMatcher(load_tickers(args.tickers, Path(args.ref_dir)))
    years = (args.from_year, args.to_year)
    for sub in args.subs:
        run_sub_pipeline(sub, files, torrent_path, raw_dir, corpus_dir, matcher, years,
                         keep_raw=args.keep_raw, comments=not args.posts_only)
    return 0


def cmd_preview(args) -> int:
    matcher = TickerMatcher(load_tickers(args.tickers, Path(args.ref_dir)))
    if args.file:
        records = iter_records(Path(args.file))
    else:
        lo = datetime.fromisoformat(args.month + "-01").replace(tzinfo=timezone.utc)
        hi = next(w for _, w in _month_windows(lo, lo + timedelta(days=32)))
        records = fetch_window(lambda e, p: _arctic_get(e, p, args.delay), "posts/search",
                               args.sub, int(lo.timestamp()), int(hi.timestamp()), 100)
    stats: Counter = Counter()
    kept, dropped = [], []
    for i, rec in enumerate(records):
        if i >= args.n:
            break
        keep, reason, tickers = is_relevant_post(rec, matcher)
        stats[("kept:" if keep else "dropped:") + reason] += 1
        line = f"[{reason}{' ' + ','.join(sorted(tickers)) if tickers else ''}] {rec.get('title', '')[:110]}"
        (kept if keep else dropped).append(line)
    _print_stats("posts", stats)
    rng = random.Random(0)
    for label, rows in (("KEPT", kept), ("DROPPED", dropped)):
        print(f"\n--- sample {label} ---")
        for line in rng.sample(rows, min(args.samples, len(rows))):
            print("  " + line)
    return 0


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


def fetch_window(fetch, endpoint: str, sub: str | None, start: int, end: int, limit: int,
                 extra: dict | None = None):
    """Yield every item in [start, end) from a created_utc-ascending search API.

    Pages by the last-seen timestamp: re-queries from one second before it and
    de-duplicates by id, so it works whether the API's ``after`` is inclusive
    or exclusive. Only fails if more than ``limit`` items share ~2 seconds, in
    which case it warns and skips ahead one second.
    """
    cursor = start
    seen_at_cursor: set[str] = set()
    while cursor < end:
        params = {"after": cursor - 1, "before": end, "sort": "asc", "limit": limit}
        if sub:
            params["subreddit"] = sub
        params.update(extra or {})
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
    """Filtered month-by-month fetch from Arctic Shift.

    Posts: every post in the month is fetched, mention-counted and filtered.
    Comments: fetched *only* for kept threads (via ``link_id``), covering the
    first ``--comment-days`` days of each thread. Note: comment mention counts
    therefore only cover kept threads on this path.
    """
    after = datetime.fromisoformat(args.after).replace(tzinfo=timezone.utc)
    before = datetime.fromisoformat(args.before).replace(tzinfo=timezone.utc)
    now = int(time.time())
    matcher = TickerMatcher(load_tickers(args.tickers, Path(args.ref_dir)))

    def fetch(endpoint, params):
        return _arctic_get(endpoint, params, args.delay)

    for sub in args.subs:
        sub_dir = Path(args.out) / sub
        for lo, hi in _month_windows(after, before):
            tag = f"{lo:%Y-%m}"
            done = sub_dir / f"mentions_{tag}.csv"  # written last = month complete
            if done.exists():
                print(f"[{sub} {tag}] done; skipping")
                continue
            counter, stats = MentionCounter(), Counter()
            kept: list[tuple[str, int]] = []
            with ZstLineWriter(sub_dir / f"posts_{tag}.zst") as w:
                posts = fetch_window(fetch, "posts/search", sub, int(lo.timestamp()),
                                     int(hi.timestamp()), 100)
                for p in posts:
                    counter.add(p["created_utc"], sub,
                                matcher.match(p.get("title")) | matcher.match(p.get("selftext")),
                                "post")
                    keep, reason, tickers = is_relevant_post(p, matcher)
                    stats[("kept:" if keep else "dropped:") + reason] += 1
                    if keep:
                        w.write(slim(p, POST_FIELDS, tickers))
                        kept.append((p["id"], int(p["created_utc"])))
            n_comments = 0
            if not args.no_comments and kept:
                with ZstLineWriter(sub_dir / f"comments_{tag}.zst") as w:
                    for pid, pts in kept:
                        end = min(pts + args.comment_days * 86400, now)
                        for c in fetch_window(fetch, "comments/search", None, pts, end, 100,
                                              extra={"link_id": pid}):
                            counter.add(c["created_utc"], sub, matcher.match(c.get("body")),
                                        "comment")
                            w.write(slim(c, COMMENT_FIELDS))
                    n_comments = w.n
            counter.write_csv(done)
            print(f"[{sub} {tag}] {len(kept):,} posts kept, {n_comments:,} comments")
            _print_stats("posts", stats)
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

    def subs(p, kinds=False):
        p.add_argument("--subs", nargs="+", default=DEFAULT_SUBS)
        if kinds:
            p.add_argument("--kinds", nargs="+", choices=KINDS, default=list(KINDS))

    def torrent(p):
        p.add_argument("--torrent", default=TORRENT_URL, help=".torrent path or URL")

    def tickers(p):
        p.add_argument("--tickers", help="ticker list file, one per line (default: SEC list)")
        p.add_argument("--ref-dir", default="data/ref", help="cache for the SEC ticker list")

    p = sp.add_parser("list", help="print exact sizes from torrent metadata")
    subs(p, kinds=True)
    torrent(p)
    p.set_defaults(func=cmd_list)

    p = sp.add_parser("pipeline", help="download + filter one subreddit file at a time")
    subs(p)
    torrent(p)
    tickers(p)
    p.add_argument("--raw-dir", default="data/raw/reddit", help="temporary raw downloads")
    p.add_argument("--out", default="data/corpus/reddit", help="filtered output")
    p.add_argument("--from-year", type=int, default=2005)
    p.add_argument("--to-year", type=int, default=2024)
    p.add_argument("--keep-raw", action="store_true", help="don't delete raw files")
    p.add_argument("--posts-only", action="store_true", help="skip comment files")
    p.set_defaults(func=cmd_pipeline)

    p = sp.add_parser("preview", help="show what the filter keeps/drops on a sample")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="a local *_submissions.zst")
    src.add_argument("--sub", help="fetch one month of this sub from Arctic Shift")
    p.add_argument("--month", default="2024-06", help="YYYY-MM, with --sub")
    p.add_argument("--n", type=int, default=5000, help="posts to examine")
    p.add_argument("--samples", type=int, default=15)
    p.add_argument("--delay", type=float, default=1.0)
    tickers(p)
    p.set_defaults(func=cmd_preview)

    p = sp.add_parser("arctic", help="filtered fetch of a date range from Arctic Shift")
    subs(p)
    tickers(p)
    p.add_argument("--after", default="2025-01-01", help="ISO date, inclusive")
    p.add_argument("--before", required=True, help="ISO date, exclusive")
    p.add_argument("--out", default="data/corpus/reddit_arctic")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--comment-days", type=int, default=180,
                   help="fetch comments made within N days of the post")
    p.set_defaults(func=cmd_arctic)

    p = sp.add_parser("torrent", help="plain download of whole files (no filtering)")
    subs(p, kinds=True)
    torrent(p)
    p.add_argument("--out", default="data/raw/reddit")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_torrent)

    p = sp.add_parser("stats", help="per-year record/byte breakdown of .zst files")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
