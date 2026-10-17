#!/usr/bin/env python3
"""Security master: which company was ticker X on date Y?

Reddit says "BBBY" in 2022 and means the old Bed Bath & Beyond (CIK 886158). The code BBBY
today belongs to a different company (CIK 1130713), and a price vendor keyed by ticker
will happily return the wrong one. So companies are identified by CIK/FIGI, and a ticker
only means something *as of a date*.

Massive's reference API can list every ticker as of any past date (``date=``), with the
company's CIK and FIGI. This module pulls those snapshots on a date grid into Parquet:

    data/market/massive/snapshots/<YYYY-MM-DD>.parquet

Commands:

* ``snapshots`` - pull the grid (resumable; one finished date = one file). The free plan
  allows 5 calls a minute, so this is slow (about 12 s per page) and meant to run in the
  background.

The key is ``MASSIVE_API_KEY`` in the environment or ``.env``; it is never printed.

``SecurityMaster`` answers the two questions the backtest asks, from those snapshots:

* ``resolve(ticker, on)``  - the company (CIK) that held the ticker on that date, with a
  confidence flag. Identity comes only from the snapshot at or before ``on``; the next
  snapshot is used solely to flag that the ticker may have changed hands in between.
* ``price_code(cik, on)``  - which EODHD price series is that company's. EODHD files a
  series under the code's *current* holder (so ``BBBY`` is Overstock's history), and the
  old holder's series sits under its final code (``BBBYQ``). A series that runs past the
  company's own lifetime belongs to someone else and is rejected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

API = "https://api.massive.com"
PAGE = 1000
MIN_INTERVAL = 12.5  # seconds between calls: the free plan allows 5 a minute

SNAPSHOT_SCHEMA = pa.schema([
    ("as_of", pa.string()), ("ticker", pa.string()), ("name", pa.string()),
    ("cik", pa.string()), ("composite_figi", pa.string()), ("share_class_figi", pa.string()),
    ("type", pa.string()), ("market", pa.string()), ("primary_exchange", pa.string()),
    ("active", pa.bool_()), ("delisted_utc", pa.string()), ("last_updated_utc", pa.string()),
])


def load_key(env_path: Path | None = None) -> str:
    key = os.environ.get("MASSIVE_API_KEY")
    if key:
        return key
    path = env_path or Path(__file__).resolve().parents[1] / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MASSIVE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    raise SystemExit("MASSIVE_API_KEY is not set (environment or .env)")


class MassiveError(RuntimeError):
    pass


class Massive:
    """Rate-limited client. Retries 429/5xx; never leaks the key into errors."""

    def __init__(self, key: str, *, opener=None, sleep=time.sleep, clock=time.monotonic,
                 min_interval: float = MIN_INTERVAL, retries: int = 6):
        self._key, self._open, self._sleep, self._clock = key, opener or self._urlopen, sleep, clock
        self._min_interval, self._retries, self._last = min_interval, retries, None

    @staticmethod
    def _urlopen(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "redditstockscraper"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _wait(self):
        if self._last is not None:
            gap = self._min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()

    def get(self, url: str) -> dict:
        """``url`` is absolute (a ``next_url`` from a previous page) or relative to the API."""
        if not url.startswith("http"):
            url = f"{API}/{url}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}apiKey={self._key}" if "apiKey=" not in url else url
        delay = 15.0
        for attempt in range(self._retries + 1):
            self._wait()
            try:
                status, body = self._open(url)
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                status, body = 599, b""
            if status == 200:
                return json.loads(body)
            if status in (429, 500, 502, 503, 504, 599) and attempt < self._retries:
                self._sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise MassiveError(f"HTTP {status} {body[:200].decode(errors='replace')}"
                               .replace(self._key, "***"))
        raise MassiveError("gave up")


def snapshot_rows(as_of: str, results: list[dict]) -> list[dict]:
    return [{
        "as_of": as_of, "ticker": r.get("ticker"), "name": r.get("name"), "cik": r.get("cik"),
        "composite_figi": r.get("composite_figi"), "share_class_figi": r.get("share_class_figi"),
        "type": r.get("type"), "market": r.get("market"),
        "primary_exchange": r.get("primary_exchange"), "active": r.get("active"),
        "delisted_utc": r.get("delisted_utc"), "last_updated_utc": r.get("last_updated_utc"),
    } for r in results]


def pull_snapshot(client: Massive, as_of: str, log=print) -> pa.Table:
    """Every stock-market ticker as of ``as_of``, following ``next_url`` pages."""
    url = f"v3/reference/tickers?market=stocks&date={as_of}&limit={PAGE}&order=asc&sort=ticker"
    rows, pages = [], 0
    while url:
        data = client.get(url)
        rows += snapshot_rows(as_of, data.get("results") or [])
        pages += 1
        url = data.get("next_url")
        if pages % 5 == 0:
            log(f"    {as_of}: {pages} pages, {len(rows):,} tickers")
    return pa.Table.from_pylist(rows, schema=SNAPSHOT_SCHEMA)


def write_atomic(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def grid_dates(first_year: int, last_year: int, per_year: int = 1) -> list[str]:
    """Jan 4 (and Jul 4 when ``per_year`` is 2) of each year: far enough into the year that
    the new-year ticker changes have landed, and a weekday is not required by the API."""
    months = (1, 7)[:per_year]
    return [f"{y}-{m:02d}-04" for y in range(first_year, last_year + 1) for m in months
            if date(y, m, 4) <= date.today()]


def snapshot_path(root: Path, as_of: str) -> Path:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        raise ValueError(as_of)
    return root / "snapshots" / f"{as_of}.parquet"


@dataclass(frozen=True)
class Resolution:
    cik: str | None
    name: str | None
    ticker: str
    confidence: str  # exact | stable | changed_between | from_later_snapshot | unknown
    snapshot: str | None  # as_of of the snapshot the answer came from


BANKRUPT_SUFFIXES = ("Q",)  # BBBY -> BBBYQ after a Chapter 11 filing


class SecurityMaster:
    """Point-in-time ticker -> company lookups over the cached Massive snapshots.

    ``snapshots`` is a list of dict rows (as_of, ticker, name, cik, active, ...);
    ``price_index`` maps an EODHD code to ``(first_date, last_date)`` of its price series.
    """

    def __init__(self, snapshots: list[dict], price_index: dict[str, tuple[str, str]] | None = None):
        self.dates = sorted({r["as_of"] for r in snapshots})
        self._by_date: dict[str, dict[str, dict]] = {d: {} for d in self.dates}
        self._ciks: dict[str, dict] = {}
        self._holders: dict[str, list[tuple[str, str]]] = {}  # ticker -> [(as_of, cik)]
        for r in snapshots:
            if not r.get("ticker"):
                continue
            self._by_date[r["as_of"]].setdefault(r["ticker"], r)  # first row wins on dupes
            c = r.get("cik")
            if c:
                self._holders.setdefault(r["ticker"], []).append((r["as_of"], c))
                info = self._ciks.setdefault(c, {"first": r["as_of"], "last": r["as_of"],
                                                 "tickers": {}, "name": r.get("name")})
                info["first"], info["last"] = min(info["first"], r["as_of"]), max(info["last"], r["as_of"])
                info["name"] = r.get("name") or info["name"]
                info["tickers"].setdefault(r["ticker"], []).append(r["as_of"])
        self.price_index = price_index or {}

    @classmethod
    def load(cls, massive_root: Path, price_index: dict | None = None) -> "SecurityMaster":
        rows: list[dict] = []
        for f in sorted((Path(massive_root) / "snapshots").glob("*.parquet")):
            rows += pq.read_table(f).to_pylist()
        return cls(rows, price_index)

    # ------------------------------------------------------------ identity

    def resolve(self, ticker: str, on: date | str) -> Resolution:
        on = str(on)
        i = bisect_right(self.dates, on)  # snapshots[:i] are at or before `on`
        prev = self.dates[i - 1] if i else None
        nxt = self.dates[i] if i < len(self.dates) else None
        before = self._by_date[prev].get(ticker) if prev else None
        after = self._by_date[nxt].get(ticker) if nxt else None
        if before is not None:
            if prev == on:
                conf = "exact"
            elif nxt is None:
                conf = "after_last_snapshot"  # nothing later to compare against
            elif after is not None and after.get("cik") == before.get("cik"):
                conf = "stable"  # same company on both sides of `on`
            else:
                conf = "changed_between"  # delisted or reassigned before the next snapshot
            return Resolution(before.get("cik"), before.get("name"), ticker, conf, prev)
        if after is not None:  # listed between the two snapshots
            return Resolution(after.get("cik"), after.get("name"), ticker,
                              "from_later_snapshot", nxt)
        return Resolution(None, None, ticker, "unknown", prev)

    def lifetime_end(self, cik: str, _seen: frozenset = frozenset()) -> str | None:
        """None while the company (or its successor) is still in the newest snapshot;
        otherwise the first snapshot date at which its ticker is gone.

        A *successor* carries the same listing across a CIK change (Google -> Alphabet,
        a SPAC merger): its ticker sits in the very next snapshot under a new CIK, and
        the price series continues. A ticker that is absent for at least one snapshot
        before reappearing is *reuse* (BBBY), and the later series is someone else's."""
        info = self._ciks.get(cik)
        if info is None or info["last"] == self.dates[-1]:
            return None
        nxt = self.dates[bisect_right(self.dates, info["last"])]
        for ticker, seen in info["tickers"].items():
            if info["last"] in seen:
                successor = self._by_date[nxt].get(ticker)
                if successor and successor.get("cik") and successor["cik"] != cik \
                        and successor["cik"] not in _seen:
                    return self.lifetime_end(successor["cik"], _seen | {cik})
        return nxt

    # ------------------------------------------------------------ prices

    def reused_after(self, code: str, cik: str, after: str) -> bool:
        """Does a *different* company hold ``code`` in a snapshot at or after ``after``?"""
        return any(d >= after and k != cik for d, k in self._holders.get(code, ()))

    def price_code(self, cik: str, on: date | str) -> tuple[str | None, str]:
        """(EODHD code, reason). Candidates are the tickers the company ever held plus
        their bankruptcy-suffixed forms. A code that a different company takes over after
        this one's lifetime holds that company's history (BBBY), so it is rejected. A series
        that merely runs on past the exchange listing (OTC trading) is still the company's."""
        on = str(on)
        info = self._ciks.get(cik)
        if info is None:
            return None, "unknown_cik"
        end = self.lifetime_end(cik)
        held_on = {t for t, ds in info["tickers"].items() if any(d <= on for d in ds)}
        cands = list(dict.fromkeys(
            list(info["tickers"]) + [t + s for t in info["tickers"] for s in BANKRUPT_SUFFIXES]))
        ok, why = [], []
        for c in cands:
            span = self.price_index.get(c)
            if span is None:
                continue
            if end is not None and self.reused_after(c, cik, end):
                why.append(f"{c} reused by another company")
                continue
            if span[0] <= on <= span[1]:
                ok.append(c)
        if not ok:
            return None, "no_valid_series" + (f" ({'; '.join(why)})" if why else "")
        ok.sort(key=lambda c: (c not in held_on, c))  # prefer a ticker it had by then
        return ok[0], "ok"


def cmd_snapshots(args) -> int:
    root = Path(args.out)
    dates = args.dates or grid_dates(args.from_year, args.to_year, args.per_year)
    client = Massive(load_key())
    todo = [d for d in dates if not snapshot_path(root, d).exists()]
    print(f"snapshots: {len(dates)} dates, {len(dates) - len(todo)} already cached, "
          f"{len(todo)} to pull (about 12 s per page, ~10 pages per date)")
    for d in todo:
        t0 = time.time()
        table = pull_snapshot(client, d)
        write_atomic(table, snapshot_path(root, d))
        print(f"  {d}: {table.num_rows:,} tickers in {(time.time() - t0) / 60:.1f} min", flush=True)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default="data/market/massive")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshots", help="pull point-in-time ticker lists on a date grid")
    s.add_argument("--from-year", type=int, default=2010)
    s.add_argument("--to-year", type=int, default=date.today().year)
    s.add_argument("--per-year", type=int, choices=(1, 2), default=1)
    s.add_argument("--dates", nargs="+", metavar="YYYY-MM-DD", help="exact dates instead of a grid")
    args = p.parse_args(argv)
    return cmd_snapshots(args)


if __name__ == "__main__":
    sys.exit(main())
