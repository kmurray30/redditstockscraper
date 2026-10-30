#!/usr/bin/env python3
"""Convert the filtered Reddit corpus (zstd NDJSON) into typed Parquet for DuckDB.

    data/corpus/reddit/<sub>/{posts,comments}.zst   (from download_reddit.py pipeline)
 -> data/corpus/parquet/posts/<sub>.parquet
    data/corpus/parquet/comments/<sub>.parquet

``edited_utc`` is when the item was last edited (0 = never, -1 = at an unknown time): a
backtest must not trust text edited after its maturation window. Comment ``link_id`` ("t3_abc") becomes ``post_id`` ("abc"). ``created_utc`` is an integer in
both tables (the dumps store it as a string in places). ``score`` / ``num_comments`` /
``upvote_ratio`` are *final* values, not point-in-time: they are kept for reference but
no backtest feature may use them. Deleted/removed text is already dropped by the filter.
Stays private: never commit or publish this data.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_reddit import iter_records  # noqa: E402

POST_SCHEMA = pa.schema([
    ("id", pa.string()), ("subreddit", pa.string()), ("created_utc", pa.int64()),
    ("author", pa.string()), ("title", pa.string()), ("selftext", pa.string()),
    ("score", pa.int64()), ("upvote_ratio", pa.float64()), ("num_comments", pa.int64()),
    ("flair", pa.string()), ("url", pa.string()), ("domain", pa.string()),
    ("is_self", pa.bool_()), ("matched_tickers", pa.list_(pa.string())),
    ("edited_utc", pa.int64()),
])
COMMENT_SCHEMA = pa.schema([
    ("id", pa.string()), ("post_id", pa.string()), ("parent_id", pa.string()),
    ("created_utc", pa.int64()), ("author", pa.string()), ("body", pa.string()),
    ("score", pa.int64()), ("edited_utc", pa.int64()),
])
CHUNK = 100_000


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v):
    return None if v is None else v if isinstance(v, str) else str(v)


def edited_utc(v) -> int:
    """0 = never edited; a timestamp when Reddit recorded one; -1 = edited at an unknown
    time (old dumps store just ``true``), which callers must treat as 'edited after'."""
    if v in (None, False, 0, "0", ""):
        return 0
    if v is True:
        return -1
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return -1


def post_row(r: dict) -> dict:
    return {
        "id": _str(r.get("id")), "subreddit": _str(r.get("subreddit")),
        "created_utc": _int(r.get("created_utc")),
        "author": _str(r.get("author")), "title": _str(r.get("title")) or "",
        "selftext": _str(r.get("selftext")) or "",
        "score": _int(r.get("score")), "upvote_ratio": _float(r.get("upvote_ratio")),
        "num_comments": _int(r.get("num_comments")), "flair": _str(r.get("link_flair_text")),
        "url": _str(r.get("url")), "domain": _str(r.get("domain")), "is_self": bool(r.get("is_self")),
        "matched_tickers": sorted(map(str, r.get("matched_tickers") or [])),
        "edited_utc": edited_utc(r.get("edited")),
    }


def comment_row(r: dict) -> dict:
    link = _str(r.get("link_id")) or ""
    return {
        "id": _str(r.get("id")), "post_id": link.split("_", 1)[-1],
        "parent_id": _str(r.get("parent_id")), "created_utc": _int(r.get("created_utc")),
        "author": _str(r.get("author")), "body": _str(r.get("body")) or "",
        "score": _int(r.get("score")), "edited_utc": edited_utc(r.get("edited")),
    }


def convert(src: Path, dst: Path, row_fn, schema: pa.Schema) -> int:
    """Stream ``src`` into ``dst`` in chunks; atomic via a temp file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    n, buf = 0, []
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w:
        for rec in iter_records(src):
            buf.append(row_fn(rec))
            if len(buf) >= CHUNK:
                w.write_table(pa.Table.from_pylist(buf, schema=schema))
                n, buf = n + len(buf), []
        if buf:
            w.write_table(pa.Table.from_pylist(buf, schema=schema))
            n += len(buf)
    os.replace(tmp, dst)
    return n


ARCTIC_TAG = "__arctic"   # posts/<sub>__arctic.parquet: the Arctic Shift backfill (2025+)


def concat_months(files: list[Path], dst: Path, row_fn, schema: pa.Schema) -> int:
    """Several monthly zstd NDJSON files into one Parquet file; atomic via a temp file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    n = 0
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w:
        for f in files:
            buf = []
            for rec in iter_records(f):
                buf.append(row_fn(rec))
                if len(buf) >= CHUNK:
                    w.write_table(pa.Table.from_pylist(buf, schema=schema))
                    n, buf = n + len(buf), []
            if buf:
                w.write_table(pa.Table.from_pylist(buf, schema=schema))
                n += len(buf)
    os.replace(tmp, dst)
    return n


def build_arctic(src_root: Path, out_root: Path, log=print) -> dict:
    """``download_reddit.py arctic`` writes ``<sub>/posts_YYYY-MM.zst`` and ``comments_YYYY-MM.zst``
    per finished month (``mentions_YYYY-MM.csv`` marks it finished). They become
    ``posts/<sub>__arctic.parquet`` and ``comments/<sub>__arctic.parquet`` next to the dump files,
    so every reader that globs ``posts/*.parquet`` sees 2025+ too. Unfinished months are skipped."""
    counts = {}
    if not src_root.exists():
        return counts
    for sub_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
        done = {f.stem.split("_", 1)[1] for f in sub_dir.glob("mentions_*.csv")}
        for kind, fn, schema in (("posts", post_row, POST_SCHEMA), ("comments", comment_row, COMMENT_SCHEMA)):
            files = sorted(f for f in sub_dir.glob(f"{kind}_*.zst") if f.stem.split("_", 1)[1] in done)
            if not files:
                continue
            dst = out_root / kind / f"{sub_dir.name}{ARCTIC_TAG}.parquet"
            if dst.exists() and dst.stat().st_mtime >= max(f.stat().st_mtime for f in files):
                log(f"  {sub_dir.name} {kind} (arctic): up to date")
                continue
            counts[(sub_dir.name, kind)] = concat_months(files, dst, fn, schema)
            log(f"  {sub_dir.name} {kind} (arctic, {len(files)} months): {counts[(sub_dir.name, kind)]:,} rows")
    return counts


def build(src_root: Path, out_root: Path, subs: list[str] | None = None, log=print) -> dict:
    counts = {}
    for sub_dir in sorted(p for p in src_root.iterdir() if (p / "manifest.json").exists()):
        sub = sub_dir.name
        if subs and sub not in subs:
            continue
        for kind, fn, schema in (("posts", post_row, POST_SCHEMA),
                                 ("comments", comment_row, COMMENT_SCHEMA)):
            src = sub_dir / f"{kind}.zst"
            if not src.exists():
                continue
            dst = out_root / kind / f"{sub}.parquet"
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                log(f"  {sub} {kind}: up to date")
                continue
            counts[(sub, kind)] = convert(src, dst, fn, schema)
            log(f"  {sub} {kind}: {counts[(sub, kind)]:,} rows")
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--src", default="data/corpus/reddit")
    p.add_argument("--out", default="data/corpus/parquet")
    p.add_argument("--subs", nargs="+")
    p.add_argument("--arctic", default="data/corpus/reddit_arctic",
                   help="the Arctic Shift backfill's monthly files (skipped when absent)")
    a = p.parse_args(argv)
    build(Path(a.src), Path(a.out), a.subs)
    build_arctic(Path(a.arctic), Path(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
