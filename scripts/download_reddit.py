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
    DISCUSSED_LINK_MIN_COMMENTS,
    FILTER_VERSION,
    POST_FIELDS,
    MentionCounter,
    TickerMatcher,
    is_empty_comment,
    is_relevant_post,
    load_stoplist,
    load_tickers,
    slim,
    stoplist_fingerprint,
)

try:  # ~3-5x faster JSON parsing on big comment files, if installed
    import orjson

    _loads = orjson.loads
except ImportError:  # pragma: no cover
    _loads = json.loads

TORRENT_HASH = "1614740ac8c94505e4ecb9d88be8bed7b6afddd4"  # subreddits, 2005-06..2024-12
TORRENT_URL = f"https://academictorrents.com/download/{TORRENT_HASH}.torrent"
TORRENT_PAGE = f"https://academictorrents.com/details/{TORRENT_HASH}"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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
    """Return ``(aria2_index, path, size)`` for each file; index is 1-based.

    ``path`` is relative to the download directory. For multi-file torrents aria2c
    saves everything under a folder named after the torrent, so that name is the
    first path component.
    """
    info = bdecode(torrent_bytes)[b"info"]
    name = info[b"name"].decode("utf-8", "replace")
    if b"files" not in info:
        return [(1, name, info[b"length"])]
    files = []
    for idx, f in enumerate(info[b"files"], start=1):
        parts = [p.decode("utf-8", "replace") for p in f[b"path"]]
        files.append((idx, "/".join([name, *parts]), f[b"length"]))
    return files


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        body = ""
    return f"HTTP {e.code} {e.reason}" + (f": {body[:500]}" if body else "")


def _torrent_url_candidates(src: str) -> list[tuple[str, str]]:
    """(url, user-agent) pairs to try. The site is picky, so vary both."""
    urls = [src]
    if src.endswith(".torrent"):
        urls.append(src[: -len(".torrent")])  # some mirrors serve it without extension
    return [(u, ua) for ua in (USER_AGENT, BROWSER_UA) for u in urls]


def _fetch_magnet_metadata(info_hash: str, out: Path) -> bytes | None:
    """Get the .torrent file list from the swarm via aria2 (no website needed)."""
    aria2 = shutil.which("aria2c")
    if not aria2:
        return None
    out.mkdir(parents=True, exist_ok=True)
    magnet = (
        f"magnet:?xt=urn:btih:{info_hash}"
        "&tr=https%3A%2F%2Facademictorrents.com%2Fannounce.php"
        "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
    )
    print("  trying magnet link via aria2c (can take a minute)...")
    cmd = [aria2, "--bt-metadata-only=true", "--bt-save-metadata=true",
           "--follow-torrent=false", "--bt-stop-timeout=120", f"--dir={out}", magnet]
    try:
        subprocess.run(cmd, check=False, timeout=300, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    saved = out / f"{info_hash}.torrent"
    return saved.read_bytes() if saved.exists() else None


def load_torrent(src: str, cache_dir: Path | None = None) -> bytes:
    """Load a .torrent from a path or URL; for URLs, fall back to a magnet link."""
    if not src.startswith(("http://", "https://")):
        return Path(src).read_bytes()
    errors = []
    for url, ua in _torrent_url_candidates(src):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            bdecode(data)  # reject HTML error pages served with a 200
            return data
        except urllib.error.HTTPError as e:
            errors.append(f"{url}: {_http_error_text(e)}")
        except (urllib.error.URLError, TimeoutError, ValueError, IndexError) as e:
            errors.append(f"{url}: {e}")
    info_hash = next((h for h in [src.rsplit("/", 1)[-1].removesuffix(".torrent")]
                      if len(h) == 40), None)
    if info_hash:
        data = _fetch_magnet_metadata(info_hash, cache_dir or Path("data/raw/reddit"))
        if data:
            return data
    raise RuntimeError(
        "could not fetch the torrent file:\n  " + "\n  ".join(errors) + "\n\n"
        "Workaround: open " + TORRENT_PAGE + " in a browser, download the .torrent, then pass "
        "--torrent /path/to/file.torrent (install aria2 for the magnet-link fallback)."
    )


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
    files = torrent_files(_ensure_torrent(args.torrent, Path(args.raw_dir)).read_bytes())
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
        path.write_bytes(load_torrent(src, out))
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
        "--console-log-level=warn",  # hide routine tracker/peer noise; keep real errors
        "--summary-interval=60",
        f"--dir={out}",
        str(torrent_path),
    ]
    print(
        "  Note: lines saying 'Torrent not registered' or 'Checksum error' are normal. It is\n"
        "  working while the progress line (DL: speed, ETA) keeps moving. A small file can show a\n"
        "  bigger total because torrents transfer whole pieces. Ctrl-C is safe; rerun to resume."
    )
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


def filter_posts(records, sub, matcher, counter, years, writer, stats,
                 min_link_comments=DISCUSSED_LINK_MIN_COMMENTS) -> set[str]:
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
        keep, reason, tickers = is_relevant_post(rec, matcher, min_link_comments)
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
        if is_empty_comment(rec):
            stats["dropped_empty"] += 1
            continue
        if years[0] <= _year(ts) <= years[1]:
            counter.add(ts, sub, matcher.match(rec.get("body")), "comment")
        link = (rec.get("link_id") or "").removeprefix("t3_")
        if link in kept_posts:
            writer.write(slim(rec, COMMENT_FIELDS))
            stats["kept"] += 1
        else:
            stats["dropped"] += 1


def _make_matcher(args) -> TickerMatcher:
    return TickerMatcher(load_tickers(args.tickers, Path(args.ref_dir)),
                         load_stoplist(args.stoplist))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    tmp.replace(path)


def _aria2_sidecar(raw: Path) -> Path:
    return raw.with_name(raw.name + ".aria2")


def _marker(raw: Path) -> Path:
    """Written by us once a file is verified and moved into place."""
    return raw.with_name(raw.name + ".ok")


def _workdir(raw_dir: Path, sub: str, kind: str) -> Path:
    """Private scratch folder for one fetch. aria2c's progress file, resume state and the
    bytes it writes for neighbouring files all stay in here and are deleted together."""
    return raw_dir / ".work" / f"{sub}_{kind}"


def _control_files(folder: Path, rel: str) -> list[Path]:
    """aria2c's progress files in ``folder``.

    A multi-file torrent has ONE control file for the whole torrent, named after its
    top-level folder (``<folder>/<top>.aria2``); single files get ``<file>.aria2``.
    """
    top = Path(rel).parts[0]
    candidates = [folder / f"{top}.aria2", _aria2_sidecar(folder / rel)]
    return [c for c in dict.fromkeys(candidates) if c.exists()]


def _locate(raw_dir: Path, rel: str) -> Path | None:
    """Where aria2c put the file: the expected path, else search by file name."""
    expected = raw_dir / rel
    if expected.exists():
        return expected
    found = sorted(raw_dir.rglob(Path(rel).name)) if raw_dir.exists() else []
    return found[0] if found else None


def _size_ok(raw: Path | None, size: int) -> bool:
    return raw is not None and raw.is_file() and raw.stat().st_size == size


def _fetch_raw(sub, kind, files, torrent_path, raw_dir) -> Path | None:
    """Return a verified local copy of ``<sub>_<kind>.zst``, downloading it if needed.

    Each download happens in its own scratch folder. aria2c refuses to start in a torrent
    folder that already exists without its progress file, and it downloads whole pieces, so
    it also writes the edges of the neighbouring files, which then look like real files
    (sparse, even full length). Isolating the fetch avoids both: only a file that aria2c
    finished, whose size matches, is moved into ``raw_dir`` and given an ``.ok`` marker, and
    everything else is deleted with the scratch folder.
    """
    chosen, _ = select_files(files, [sub], [kind])
    if not chosen:
        print(f"  {sub}_{kind}.zst is not in the torrent; skipping")
        return None
    idx, rel, size = chosen[0]
    final = raw_dir / rel
    if _size_ok(final, size) and _marker(final).exists():
        print(f"  using existing {final}")
        return final

    work = _workdir(raw_dir, sub, kind)
    if work.exists() and not _control_files(work, rel):
        shutil.rmtree(work)  # leftovers with no progress record: can't be resumed
    if work.exists():
        print(f"  resuming {Path(rel).name} ({human(size)})")
    else:
        print(f"  downloading {Path(rel).name} ({human(size)})")
    work.mkdir(parents=True, exist_ok=True)

    rc = _aria2_fetch(torrent_path, [idx], work)
    got = _locate(work, rel)
    if rc == 0 and not _size_ok(got, size):
        # aria2c says "done" but the file isn't there or is short: its progress record is
        # out of step with the disk. Start this one fetch over in a clean folder.
        print("  aria2c reported success without the complete file; starting this file over.")
        shutil.rmtree(work)
        work.mkdir(parents=True)
        rc = _aria2_fetch(torrent_path, [idx], work)
        got = _locate(work, rel)
    if rc != 0 or not _size_ok(got, size):
        actual = got.stat().st_size if got is not None and got.is_file() else "missing"
        controls = ", ".join(c.name for c in _control_files(work, rel)) or "none"
        raise RuntimeError(
            f"download of {Path(rel).name} did not finish (aria2c exit code {rc}).\n"
            f"  expected size: {size:,}   on disk: {actual if actual == 'missing' else f'{actual:,}'}\n"
            f"  aria2c control files present: {controls}\n"
            "  Rerun the same command to resume. A tracker message such as 'Torrent not "
            "registered' is usually harmless; peers also come from other sources."
        )

    final.parent.mkdir(parents=True, exist_ok=True)
    final.unlink(missing_ok=True)
    shutil.move(str(got), str(final))
    _marker(final).write_text(str(size))
    shutil.rmtree(work, ignore_errors=True)  # spill, control file and all
    return final


def sweep_untrusted(files, folder: Path) -> tuple[int, int]:
    """Delete every file of the torrent in ``folder`` that has no ``.ok`` marker.

    Older versions downloaded straight into ``raw_dir``, leaving unverified neighbour spill
    there. Size can't identify spill (it may have its full length while mostly holes), so
    only marked files are trusted. Returns ``(files_removed, bytes_freed)``.
    """
    declared = {Path(rel).name for _, rel, _ in files}
    removed = freed = 0
    if not folder.is_dir():
        return 0, 0
    for f in folder.iterdir():
        if f.name in declared and f.is_file() and not _marker(f).exists():
            freed += f.stat().st_size
            f.unlink()
            removed += 1
    return removed, freed


def cmd_clean_raw(args) -> int:
    """Remove scratch folders, old progress files and unverified torrent files."""
    raw_dir = Path(args.raw_dir)
    files = torrent_files(_ensure_torrent(args.torrent, raw_dir).read_bytes())
    work_root = raw_dir / ".work"
    tops = {Path(rel).parts[0] for _, rel, _ in files}
    interrupted = [c for w in (work_root.iterdir() if work_root.is_dir() else [])
                   for c in w.rglob("*.aria2")]
    if interrupted and not args.force:
        names = sorted({c.relative_to(work_root).parts[0] for c in interrupted})
        print("A download was interrupted (" + ", ".join(names) + ").\n"
              "Rerun `pipeline` to finish it, or use --force to throw it away and clean up.")
        return 1
    removed = freed = 0
    for f in work_root.rglob("*") if work_root.is_dir() else []:
        if f.is_file():
            freed += f.stat().st_size
    shutil.rmtree(work_root, ignore_errors=True)
    for folder in {(raw_dir / rel).parent for _, rel, _ in files}:
        r, f = sweep_untrusted(files, folder)
        removed, freed = removed + r, freed + f
    for t in tops:  # progress file left by older versions
        (raw_dir / f"{t}.aria2").unlink(missing_ok=True)
    print(f"removed {removed} unverified files, freed {human(freed)}")
    return 0


def _discard(raw: Path, keep_raw: bool, raw_dir: Path) -> None:
    """Delete a verified raw file and its marker."""
    if keep_raw:
        return
    raw.unlink(missing_ok=True)
    _marker(raw).unlink(missing_ok=True)


def _print_stats(label: str, stats: Counter) -> None:
    total = sum(stats.values())
    kept = sum(v for k, v in stats.items() if k.startswith("kept"))
    print(f"  {label}: kept {kept:,} of {total:,} ({kept / total:.1%})" if total else f"  {label}: empty")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"      {k:24} {v:>12,}")


def run_sub_pipeline(sub, files, torrent_path, raw_dir, corpus_dir, matcher, years,
                     keep_raw=False, comments=True,
                     min_link_comments=DISCUSSED_LINK_MIN_COMMENTS, stoplist_id="") -> dict:
    sub_dir = corpus_dir / sub
    sub_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"filter_version": FILTER_VERSION, "years": list(years),
             "min_link_comments": min_link_comments, "stoplist": stoplist_id}
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
            kept = filter_posts(iter_records(raw), sub, matcher, counter, years, w, post_stats,
                                min_link_comments)
        counter.write_csv(sub_dir / "mentions_posts.csv")
        _write_json(posts_stage, {**stamp, "stats": post_stats, "raw_bytes": raw.stat().st_size})
        _discard(raw, keep_raw, raw_dir)
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
            _discard(raw, keep_raw, raw_dir)
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
    matcher = _make_matcher(args)
    years = (args.from_year, args.to_year)
    for sub in args.subs:
        run_sub_pipeline(sub, files, torrent_path, raw_dir, corpus_dir, matcher, years,
                         keep_raw=args.keep_raw, comments=not args.posts_only,
                         min_link_comments=args.min_link_comments,
                         stoplist_id=stoplist_fingerprint(load_stoplist(args.stoplist)))
    return 0


def cmd_preview(args) -> int:
    matcher = _make_matcher(args)
    if args.file:
        records = iter_records(Path(args.file))
    else:
        lo = datetime.fromisoformat(args.month + "-01").replace(tzinfo=timezone.utc)
        hi = next(w for _, w in _month_windows(lo, lo + timedelta(days=32)))
        records = fetch_window(lambda e, p: _arctic_get(e, p, args.delay), "posts/search",
                               args.sub, int(lo.timestamp()), int(hi.timestamp()), 100)
    stats: Counter = Counter()
    kept, dropped = [], []
    try:
        for i, rec in enumerate(records):
            if i >= args.n:
                break
            keep, reason, tickers = is_relevant_post(rec, matcher, args.min_link_comments)
            stats[("kept:" if keep else "dropped:") + reason] += 1
            tag = reason + (" " + ",".join(sorted(tickers)) if tickers else "")
            (kept if keep else dropped).append(f"[{tag}] {rec.get('title', '')[:110]}")
    except RuntimeError as e:
        if not stats:
            raise
        print(f"\nstopped early after {sum(stats.values()):,} posts: {e}\n")
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


def _iso(value) -> str:
    """Epoch seconds -> ISO-8601 UTC. Plain integers are ambiguous (s vs ms)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return value


class _Page(list):
    """A page of results that remembers the page size actually requested."""

    limit_used: int | None = None


_arctic_penalty = 0.0  # extra seconds between requests; grows on timeouts, decays on success


def _is_transient(code: int, body: str) -> bool:
    """429 and 5xx are retryable. So is the 422 this API uses for query timeouts."""
    if code == 429 or code >= 500:
        return True
    return any(w in body.lower() for w in ("timeout", "timed out", "slow down"))


def _arctic_get(endpoint: str, params: dict, polite_delay: float) -> list[dict]:
    """GET one page, retrying transient failures. Shrinks ``limit`` after repeated timeouts
    (the API's own advice for "Query timed out"). The returned page records the size it
    actually used in ``limit_used`` so callers can tell a short page from the last page."""
    global _arctic_penalty
    params = {k: (_iso(v) if k in ("after", "before") else v) for k, v in params.items()}
    timeouts, last = 0, "no response"
    for attempt in range(8):
        url = f"{ARCTIC_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                payload = json.load(r)
            if payload.get("error"):
                raise RuntimeError(payload["error"])
            time.sleep(polite_delay + _arctic_penalty)
            _arctic_penalty *= 0.5
            page = _Page(payload.get("data") or [])
            page.limit_used = int(params.get("limit") or 0) or None
            return page
        except urllib.error.HTTPError as e:
            body = _http_error_text(e)
            if not _is_transient(e.code, body):
                raise RuntimeError(
                    f"Arctic Shift rejected the request ({body})\n  url: {url}"
                ) from e
            last = body
            if e.code == 429:
                wait = float(e.headers.get("X-RateLimit-Reset") or 30)
            else:
                timeouts += 1
                wait = min(60.0, 2.0**attempt) + random.random()
                _arctic_penalty = min(30.0, _arctic_penalty + 1.0)
                if timeouts >= 2 and int(params.get("limit", 0) or 0) > 10:
                    params["limit"] = max(10, int(params["limit"]) // 2)
            print(f"    transient error ({body[:80]}); retrying in {wait:.0f}s "
                  f"(limit={params.get('limit')})")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
            last = repr(e)
            print(f"    {e!r}; retrying")
            time.sleep(min(60.0, 2.0**attempt))
    raise RuntimeError(
        f"Arctic Shift kept failing (last: {last}). It is a free best-effort service and may "
        f"be overloaded: try again later or raise --delay.\n  url: {url}"
    )


def fetch_window(fetch, endpoint: str, sub: str | None, start: int, end: int, limit: int,
                 extra: dict | None = None):
    """Yield every item in [start, end) from a created_utc-ascending search API.

    Pages by the last-seen timestamp: re-queries from one second before it and
    de-duplicates by id, so it works whether the API's ``after`` is inclusive
    or exclusive. Only misbehaves if more than ``limit`` items share ~2 seconds, in
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
        eff_limit = getattr(page, "limit_used", None) or limit  # may have been shrunk
        fresh = [
            it
            for it in page
            if cursor <= int(it["created_utc"]) < end and it["id"] not in seen_at_cursor
        ]
        if not fresh:
            if len(page) < eff_limit:
                return
            print(f"    warning: >{eff_limit} items near ts={cursor}; some may be skipped")
            cursor, seen_at_cursor = cursor + 1, set()
            continue
        yield from fresh
        last_ts = int(fresh[-1]["created_utc"])
        if last_ts != cursor:
            seen_at_cursor = set()
            cursor = last_ts
        seen_at_cursor.update(it["id"] for it in fresh if int(it["created_utc"]) == cursor)
        if len(page) < eff_limit:
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
    matcher = _make_matcher(args)

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
                    keep, reason, tickers = is_relevant_post(p, matcher, args.min_link_comments)
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
                            if is_empty_comment(c):
                                continue
                            counter.add(c["created_utc"], sub, matcher.match(c.get("body")),
                                        "comment")
                            w.write(slim(c, COMMENT_FIELDS))
                    n_comments = w.n
            counter.write_csv(done)
            print(f"[{sub} {tag}] {len(kept):,} posts kept, {n_comments:,} comments")
            _print_stats("posts", stats)
    return 0


def cmd_report(args) -> int:
    """Sanity-check a filtered subreddit: keep rates, top tickers, sample titles, comment depth."""
    sub_dir = Path(args.out) / args.sub
    manifest = _read_json(sub_dir / "manifest.json")
    if not manifest:
        print(f"no manifest in {sub_dir}; run the pipeline for this subreddit first")
        return 1
    print(f"== {args.sub}  (filter {manifest['filter_version']}, years {manifest['years']}) ==")
    for label in ("posts", "comments"):
        counts = manifest.get(label) or {}
        total = sum(counts.values())
        kept = sum(v for k, v in counts.items() if k.startswith("kept"))
        print(f"\n{label}: kept {kept:,} of {total:,}" + (f" ({kept / total:.1%})" if total else ""))
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {k:24} {v:>10,} {v / total:>7.1%}")

    totals: Counter = Counter()
    for name in ("mentions_posts.csv", "mentions_comments.csv"):
        f = sub_dir / name
        if f.exists():
            for line in f.read_text().splitlines()[1:]:
                _, _, ticker, _, n = line.split(",")
                totals[ticker] += int(n)
    print(f"\ntop {args.top} tickers by mentions (look for English words posing as tickers):")
    print("    " + "  ".join(f"{t}:{n:,}" for t, n in totals.most_common(args.top)))

    posts_f, comments_f = sub_dir / "posts.zst", sub_dir / "comments.zst"
    if posts_f.exists():
        posts = list(iter_records(posts_f))
        rng = random.Random(0)
        print(f"\n{min(20, len(posts))} random kept posts:")
        for r in rng.sample(posts, min(20, len(posts))):
            t = ",".join(r.get("matched_tickers") or [])
            print(f"    [{_year(r['created_utc'])} {t or '-'}] {(r.get('title') or '')[:100]}")
        if comments_f.exists():
            by_year_posts, by_year_comments = Counter(), Counter()
            for r in posts:
                by_year_posts[_year(r["created_utc"])] += 1
            for r in iter_records(comments_f):
                by_year_comments[_year(r["created_utc"])] += 1
            print("\ncomments per kept post, by year (by comment date / post date):")
            for y in sorted(by_year_posts):
                ratio = by_year_comments[y] / by_year_posts[y]
                print(f"    {y}: {by_year_posts[y]:>6,} posts {by_year_comments[y]:>8,} comments"
                      f"  {ratio:>6.1f} per post")
    return 0


def cmd_check(args) -> int:
    """Poke each external service once and report PASS/FAIL, with no tracebacks."""
    ok = True

    def report(name, fn):
        nonlocal ok
        try:
            print(f"PASS  {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  {name}: {e}")

    def torrent():
        out = Path(args.raw_dir)
        files = torrent_files(_ensure_torrent(args.torrent, out).read_bytes())
        return f"{len(files):,} files, {human(sum(f[2] for f in files))} total"

    def arctic():
        rows = _arctic_get("posts/search", {"subreddit": "stocks", "after": "2021-03-01",
                                            "before": "2021-03-02", "sort": "asc",
                                            "limit": 3}, 0)
        return f"{len(rows)} posts returned"

    def sec():
        n = len(load_tickers(args.tickers, Path(args.ref_dir)))
        return f"{n:,} tickers loaded"

    report("torrent metadata (Academic Torrents)", torrent)
    report("Arctic Shift API", arctic)
    report("ticker list (SEC)", sec)
    if shutil.which("aria2c"):
        print("PASS  aria2c: installed")
    else:
        ok = False
        print("FAIL  aria2c: not found (brew install aria2)")
    return 0 if ok else 1


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
        p.add_argument("--stoplist", help="extra words that must never count as bare tickers, "
                                          "one per line (e.g. abbreviations that clash)")
        p.add_argument("--ref-dir", default="data/ref", help="cache for the SEC ticker list")

    def link_threshold(p):
        p.add_argument("--min-link-comments", type=int, default=DISCUSSED_LINK_MIN_COMMENTS,
                       help="keep link posts (articles, letters) with at least this many "
                            "comments even without a ticker (default %(default)s)")

    p = sp.add_parser("report", help="sanity-check a filtered subreddit")
    p.add_argument("--sub", required=True)
    p.add_argument("--out", default="data/corpus/reddit")
    p.add_argument("--top", type=int, default=25, help="how many top tickers to list")
    p.set_defaults(func=cmd_report)

    p = sp.add_parser("clean-raw", help="delete unverified leftover files from raw downloads")
    torrent(p)
    p.add_argument("--raw-dir", default="data/raw/reddit")
    p.add_argument("--force", action="store_true",
                   help="also discard an interrupted download's progress record")
    p.set_defaults(func=cmd_clean_raw)

    p = sp.add_parser("check", help="test each external service once (run this first)")
    torrent(p)
    tickers(p)
    p.add_argument("--raw-dir", default="data/raw/reddit")
    p.set_defaults(func=cmd_check)

    p = sp.add_parser("list", help="print exact sizes from torrent metadata")
    subs(p, kinds=True)
    torrent(p)
    p.add_argument("--raw-dir", default="data/raw/reddit", help="where the .torrent is cached")
    p.set_defaults(func=cmd_list)

    p = sp.add_parser("pipeline", help="download + filter one subreddit file at a time")
    subs(p)
    torrent(p)
    tickers(p)
    p.add_argument("--raw-dir", default="data/raw/reddit", help="temporary raw downloads")
    p.add_argument("--out", default="data/corpus/reddit", help="filtered output")
    p.add_argument("--from-year", type=int, default=2005)
    p.add_argument("--to-year", type=int, default=2024)
    link_threshold(p)
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
    link_threshold(p)
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
    link_threshold(p)
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
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted. Nothing is lost; rerun the same command to resume.",
              file=sys.stderr)
        return 130
    except (RuntimeError, urllib.error.URLError) as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
