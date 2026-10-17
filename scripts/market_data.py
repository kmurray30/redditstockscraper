#!/usr/bin/env python3
"""Bulk-pull EODHD market data into a local Parquet cache.

The security master and the backtest read this cache; nothing else talks to EODHD.

Commands:

* ``symbols`` - the US symbol lists, active and delisted, into ``symbols.parquet``.
* ``prices``  - per-symbol daily bars (open/high/low/close, adjusted_close, volume).
* ``splits`` / ``dividends`` - per-symbol corporate actions.
* ``index``   - first/last bar date and bar count per code into ``eod_index.parquet``
  (what the security master uses to tell whose price series a code really holds).

Layout under ``data/market/eodhd/``::

    symbols.parquet            one row per (code, exchange, delisted) from the lists
    eod/<CODE>.parquet         daily bars, ascending by date
    splits/<CODE>.parquet      date, ratio (new shares per old share)
    dividends/<CODE>.parquet   date, value, unadjusted_value, ...

A symbol with no data still gets a (zero-row) file, so a rerun never asks twice. Every
file is written atomically, so Ctrl-C and rerun is always safe. Symbols that the Reddit
corpus mentions are fetched first, then the major exchanges, then the OTC tail.

The key comes from ``EODHD_API_KEY`` in the environment or in ``.env``. It is never
printed. EODHD returns split-adjusted ``close`` as the price actually quoted at the time
and ``adjusted_close`` adjusted for splits *and* dividends; use ``adjusted_close`` for
returns. The same code can be reused by a different company after a delisting (BBBY), so
histories are keyed by code here and split by company in the security master later.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

API = "https://eodhd.com/api"
KINDS = ("Common Stock", "ETF")  # what the backtest universe needs; funds/bonds are noise
MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "NYSE ARCA", "NYSE MKT", "AMEX", "BATS", "US"}
EARLIEST = "1970-01-01"

EOD_SCHEMA = pa.schema([
    ("date", pa.string()), ("open", pa.float64()), ("high", pa.float64()),
    ("low", pa.float64()), ("close", pa.float64()), ("adjusted_close", pa.float64()),
    ("volume", pa.float64()),
])
SPLIT_SCHEMA = pa.schema([("date", pa.string()), ("ratio", pa.float64())])
DIV_FIELDS = ["date", "value", "unadjusted_value", "declaration_date", "record_date",
              "payment_date", "period", "currency"]
DIV_SCHEMA = pa.schema([("date", pa.string()), ("value", pa.float64()),
                        ("unadjusted_value", pa.float64())] +
                       [(f, pa.string()) for f in DIV_FIELDS[3:]])


def load_key(env_path: Path | None = None) -> str:
    """EODHD_API_KEY from the environment, else from a ``.env`` file."""
    key = os.environ.get("EODHD_API_KEY")
    if key:
        return key
    path = env_path or Path(__file__).resolve().parents[1] / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "EODHD_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    raise SystemExit("EODHD_API_KEY is not set (environment or .env)")


class EodhdError(RuntimeError):
    pass


class Eodhd:
    """Thin HTTP client: retries 429/5xx with backoff and never leaks the key."""

    def __init__(self, key: str, *, opener=None, sleep=time.sleep, retries: int = 6):
        self._key = key
        self._open = opener or self._urlopen
        self._sleep = sleep
        self._retries = retries

    @staticmethod
    def _urlopen(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "redditstockscraper"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path: str, **params):
        params = {**params, "api_token": self._key, "fmt": "json"}
        url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
        delay = 2.0
        for attempt in range(self._retries + 1):
            try:
                status, body = self._open(url)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                status, body = 599, b""
            if status == 200:
                return json.loads(body)
            if status == 404:
                return []  # unknown to EODHD: treat as "no data", don't retry
            if status in (429, 500, 502, 503, 504, 599) and attempt < self._retries:
                self._sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise EodhdError(f"{path}: HTTP {status} {body[:200].decode(errors='replace')}"
                             .replace(self._key, "***"))
        raise EodhdError(f"{path}: gave up")


# ---------------------------------------------------------------- symbols

def fetch_symbols(client: Eodhd) -> pa.Table:
    rows, now = [], datetime.now(timezone.utc).isoformat(timespec="seconds")
    for delisted in (0, 1):
        for r in client.get("exchange-symbol-list/US", delisted=delisted):
            rows.append({
                "code": r["Code"], "name": r.get("Name"), "exchange": r.get("Exchange"),
                "type": r.get("Type"), "isin": r.get("Isin"), "currency": r.get("Currency"),
                "delisted": bool(delisted), "pulled_at": now,
            })
    return pa.Table.from_pylist(rows)


def mentioned_tickers(corpus: Path) -> Counter:
    """Total Reddit mentions per ticker across every filtered subreddit."""
    totals: Counter = Counter()
    for f in corpus.glob("*/mentions_*.csv"):
        for line in f.read_text().splitlines()[1:]:
            _, _, ticker, _, n = line.split(",")
            totals[ticker] += int(n)
    return totals


def work_order(symbols: pa.Table, mentions: Counter, *, include_otc: bool = True) -> list[str]:
    """Codes to fetch: Reddit-mentioned first (most mentioned first), then major
    exchanges, then the OTC tail. Only common stock and ETFs; one entry per code."""
    best: dict[str, tuple[int, int, str]] = {}
    for r in symbols.to_pylist():
        if r["type"] not in KINDS:
            continue
        major = r["exchange"] in MAJOR_EXCHANGES
        if not major and not include_otc:
            continue
        code = r["code"]
        key = (0 if code in mentions else 1 if major else 2, -mentions.get(code, 0), code)
        if code not in best or key < best[code]:
            best[code] = key
    return [c for _, _, c in sorted(best.values())]


# ---------------------------------------------------------------- per-symbol pulls

def _eod_table(rows: list[dict]) -> pa.Table:
    rows = sorted(rows, key=lambda r: r["date"])
    return pa.Table.from_pylist(
        [{f.name: r.get(f.name) for f in EOD_SCHEMA} for r in rows], schema=EOD_SCHEMA)


def _split_table(rows: list[dict]) -> pa.Table:
    out = []
    for r in rows:
        try:
            new, old = (float(x) for x in str(r["split"]).split("/"))
            out.append({"date": r["date"], "ratio": new / old})
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    return pa.Table.from_pylist(sorted(out, key=lambda r: r["date"]), schema=SPLIT_SCHEMA)


def _div_table(rows: list[dict]) -> pa.Table:
    camel = {"unadjustedValue": "unadjusted_value", "declarationDate": "declaration_date",
             "recordDate": "record_date", "paymentDate": "payment_date"}
    out = []
    for r in sorted(rows, key=lambda r: r["date"]):
        r = {camel.get(k, k): v for k, v in r.items()}
        out.append({f.name: (float(r[f.name]) if f.type == pa.float64() and r.get(f.name)
                             is not None else r.get(f.name)) for f in DIV_SCHEMA})
    return pa.Table.from_pylist(out, schema=DIV_SCHEMA)


KIND = {
    # name -> (endpoint, extra params, table builder)
    "prices": ("eod", {"from": EARLIEST, "period": "d", "order": "a"}, _eod_table, "eod"),
    "splits": ("splits", {"from": EARLIEST}, _split_table, "splits"),
    "dividends": ("div", {"from": EARLIEST}, _div_table, "dividends"),
}


def cache_path(root: Path, folder: str, code: str) -> Path:
    return root / folder / (urllib.parse.quote(code, safe="") + ".parquet")


def write_atomic(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def fetch_one(client: Eodhd, root: Path, kind: str, code: str) -> str:
    """Fetch and cache one symbol. Returns 'cached', 'empty' or 'ok'."""
    endpoint, params, build, folder = KIND[kind]
    path = cache_path(root, folder, code)
    if path.exists():
        return "cached"
    rows = client.get(f"{endpoint}/{urllib.parse.quote(code, safe='')}.US", **params)
    if not isinstance(rows, list):
        raise EodhdError(f"{code}: unexpected response {str(rows)[:100]}")
    table = build(rows)
    write_atomic(table, path)
    return "ok" if table.num_rows else "empty"


def pull(client: Eodhd, root: Path, kind: str, codes: list[str], *, workers: int = 8,
         log=print) -> Counter:
    stats: Counter = Counter()
    lock, started = threading.Lock(), time.time()

    def work(code: str):
        try:
            res = fetch_one(client, root, kind, code)
        except EodhdError as e:
            res = "error"
            log(f"  {e}")
        with lock:
            stats[res] += 1
            done = sum(stats.values())
            if done % 500 == 0:
                rate = stats["ok"] + stats["empty"]
                log(f"  {done:,}/{len(codes):,}  fetched {rate:,}  cached {stats['cached']:,}"
                    f"  errors {stats['error']:,}  {(time.time() - started) / 60:.1f} min")

    try:
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, codes))
    except KeyboardInterrupt:
        print("\ninterrupted; rerun to resume (finished symbols are cached)")
    return stats


def build_price_index(root: Path) -> pa.Table:
    """First/last date and bar count for every cached price file (empty files omitted)."""
    files = sorted((root / "eod").glob("*.parquet"))
    rows = []
    for f in files:
        meta = pq.ParquetFile(f).metadata
        if meta.num_rows == 0:
            continue
        col = meta.row_group(0).column(0)  # `date` is the first column; stats give min/max
        lo = hi = None
        if meta.num_row_groups == 1 and col.statistics is not None and col.statistics.has_min_max:
            lo, hi = col.statistics.min, col.statistics.max
        rows.append((urllib.parse.unquote(f.stem), f, lo, hi, meta.num_rows))
    out = []
    for code, f, lo, hi, n in rows:
        if lo is None:  # no usable statistics: read the column
            dates = pq.read_table(f, columns=["date"]).column("date").to_pylist()
            lo, hi = min(dates), max(dates)
        out.append({"code": code, "first_date": lo, "last_date": hi, "bars": n})
    return pa.Table.from_pylist(out, schema=pa.schema([
        ("code", pa.string()), ("first_date", pa.string()), ("last_date", pa.string()),
        ("bars", pa.int64())]))


def load_price_index(root: Path) -> dict[str, tuple[str, str]]:
    t = pq.read_table(Path(root) / "eod_index.parquet").to_pylist()
    return {r["code"]: (r["first_date"], r["last_date"]) for r in t}


def cmd_index(args) -> int:
    root = Path(args.out)
    table = build_price_index(root)
    write_atomic(table, root / "eod_index.parquet")
    print(f"{table.num_rows:,} price series indexed -> {root / 'eod_index.parquet'}")
    return 0


# ---------------------------------------------------------------- CLI

def cmd_symbols(args) -> int:
    root = Path(args.out)
    table = fetch_symbols(Eodhd(load_key()))
    write_atomic(table, root / "symbols.parquet")
    kinds = Counter((r["type"], r["delisted"]) for r in table.to_pylist())
    print(f"{table.num_rows:,} symbols -> {root / 'symbols.parquet'}")
    for (t, d), n in kinds.most_common(8):
        print(f"    {t:16} {'delisted' if d else 'active':9} {n:>7,}")
    return 0


def cmd_pull(args) -> int:
    root = Path(args.out)
    sym = root / "symbols.parquet"
    if not sym.exists():
        print("run `symbols` first")
        return 1
    codes = work_order(pq.read_table(sym), mentioned_tickers(Path(args.corpus)),
                       include_otc=not args.no_otc)
    if args.only:
        codes = [c for c in args.only]
    elif args.universe:  # just the codes the backtest universe actually uses
        used = {r for r in pq.read_table(args.universe, columns=["code"]).column("code").to_pylist()}
        codes = [c for c in codes if c in used]
    if args.limit:
        codes = codes[:args.limit]
    print(f"{args.cmd}: {len(codes):,} symbols, {args.workers} workers")
    stats = pull(Eodhd(load_key()), root, args.cmd, codes, workers=args.workers)
    print(dict(stats))
    return 1 if stats["error"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default="data/market/eodhd", help="cache directory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("symbols", help="pull the active + delisted US symbol lists")
    sub.add_parser("index", help="index first/last bar date per cached price series")
    for name in KIND:
        s = sub.add_parser(name, help=f"pull {name} per symbol (resumable)")
        s.add_argument("--corpus", default="data/corpus/reddit",
                       help="filtered Reddit corpus, used to fetch mentioned tickers first")
        s.add_argument("--workers", type=int, default=8)
        s.add_argument("--limit", type=int, help="only the first N symbols in work order")
        s.add_argument("--only", nargs="+", metavar="CODE", help="just these codes")
        s.add_argument("--universe", metavar="PARQUET",
                       help="only codes that appear in this universe file (features/universe.parquet)")
        s.add_argument("--no-otc", action="store_true", help="skip OTC / pink-sheet symbols")
    args = p.parse_args(argv)
    if args.cmd == "symbols":
        return cmd_symbols(args)
    return cmd_index(args) if args.cmd == "index" else cmd_pull(args)


if __name__ == "__main__":
    sys.exit(main())
