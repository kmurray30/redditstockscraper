#!/usr/bin/env python3
"""Point-in-time universe and forward labels, one cohort per calendar month.

For each month start ``c`` (the cohort date; the information cutoff) and each company that
is listed around then:

* the company's price series is chosen by the security master (so BBBY 2022 is the right BBBY);
* features are measured on the last bar **before** ``c``: price, median dollar volume over
  60 bars (the size/liquidity proxy; market cap needs shares outstanding, which this
  project does not have yet), 12-1 momentum;
* ``eligible`` = common stock / ADR, price >= $1, dollar volume >= a floor;
* ``size_q`` = liquidity quintile among that month's eligible names;
* forward outcomes are measured from the open of the first bar strictly after ``c``
  (one day of lag), at several horizons (see prices.py for status/censoring).

A company is a member of month ``c`` if it appears in the snapshot just before or just
after ``c`` (the latter so recent IPOs are not missed) *and* it actually traded just before
``c``. Delisted names stay in until their last bar, so the universe has no survivorship bias.

    data/features/universe.parquet   month, cik, ticker, code, price, adv60, mom, eligible, size_q
    data/features/labels.parquet     month, cik, years, status, ret, cagr, excess, ... (long)
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices as px  # noqa: E402
from security_master import SecurityMaster  # noqa: E402

HORIZONS = (1, 3, 5, 7, 10)
STOCK_TYPES = {"CS", "ADRC"}
MIN_PRICE = 1.0
MIN_ADV = 200_000.0
BENCH = "SPY"

UNIVERSE_SCHEMA = pa.schema([
    ("month", pa.string()), ("cik", pa.string()), ("ticker", pa.string()),
    ("code", pa.string()), ("sec_type", pa.string()), ("price", pa.float64()),
    ("adv60", pa.float64()), ("mom", pa.float64()), ("eligible", pa.bool_()),
    ("size_q", pa.int8()),
])
LABEL_SCHEMA = pa.schema([
    ("month", pa.string()), ("cik", pa.string()), ("years", pa.int8()),
    ("status", pa.string()), ("ret", pa.float64()), ("cagr", pa.float64()),
    ("excess", pa.float64()), ("max_mult", pa.float64()), ("min_mult", pa.float64()),
    ("max_dd", pa.float64()), ("d2", pa.int32()), ("d5", pa.int32()), ("d10", pa.int32()),
    ("perm_loss", pa.bool_()), ("entry_day", pa.int32()), ("end_day", pa.int32()),
])


def month_starts(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last[:7]:
        out.append(f"{y:04d}-{m:02d}-01")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def memberships(master: SecurityMaster, months: list[str]) -> dict[str, dict[str, tuple]]:
    """cik -> {month: (ticker, sec_type)} for companies in the snapshot just before or just
    after each month start."""
    out: dict[str, dict[str, tuple]] = {}
    for c in months:
        i = master.snapshot_index(c)
        for as_of in {master.dates[j] for j in (i - 1, i) if 0 <= j < len(master.dates)}:
            for ticker, row in master._by_date[as_of].items():
                if row.get("type") in STOCK_TYPES and row.get("cik"):
                    out.setdefault(row["cik"], {}).setdefault(c, (ticker, row["type"]))
    return out


DROPPED_SCHEMA = pa.schema([("cik", pa.string()), ("ticker", pa.string()),
                            ("reason", pa.string()), ("months", pa.int32())])


def drop_reason(detail: str) -> str:
    """Collapse price_code's explanation into a few buckets for the summary."""
    if detail == "unknown_cik":
        return "unknown_cik"
    if "reused by another company" in detail:
        return "code_reused_after_delisting"
    if "is a different company" in detail:
        return "code_name_mismatch"
    return "no_price_series"


def build(master: SecurityMaster, store: px.PriceStore, months: list[str], *, horizons=HORIZONS,
          slippage_bps: float = 25.0, log=print,
          dropped: list[dict] | None = None) -> tuple[pa.Table, pa.Table]:
    """``dropped``, if given, receives one row per (company, reason) for members that were
    listed at a cohort date but have no usable price series, with the number of cohort
    months affected. These companies are absent from the universe *and* from every scorer's
    candidate set, so a large count (especially ``code_reused_after_delisting``, which is
    mostly dead companies) understates permanent losses on both sides of the lift."""
    bench = store.get(BENCH)
    data_end = store.data_end
    uni, labels = [], []
    members = memberships(master, months)
    drops: dict[tuple[str, str, str], int] = {}
    for n, (cik, by_month) in enumerate(members.items(), 1):
        if n % 2000 == 0:
            log(f"  {n:,}/{len(members):,} companies, {len(uni):,} universe rows")
        for c, (ticker, typ) in by_month.items():
            cday = px.to_day(c)
            code, why = master.price_code(cik, c, ticker)
            s = store.get(code)
            if s is None:
                key = (cik, ticker, drop_reason(why) if code is None else "no_price_file")
                drops[key] = drops.get(key, 0) + 1
                continue
            prev = s.index_on_or_before(cday - 1)
            if prev is None or cday - s.days[prev] > px.MAX_ENTRY_GAP:
                continue  # not trading just before the cohort date
            adv = px.trailing_dollar_volume(s, prev, 60)
            price = float(s.raw_close[prev])
            eligible = bool(adv is not None and adv >= MIN_ADV and price >= MIN_PRICE)
            uni.append({"month": c, "cik": cik, "ticker": ticker, "code": code, "sec_type": typ,
                        "price": price, "adv60": adv, "mom": px.momentum(s, prev),
                        "eligible": eligible, "size_q": 0})
            e = s.entry_index(cday + 1)
            if e is None:
                continue
            for h in horizons:
                o = px.outcome(s, e, h, data_end, bench=bench, slippage_bps=slippage_bps)
                labels.append({"month": c, "cik": cik, "years": h, "status": o.status,
                               "ret": o.ret, "cagr": o.cagr, "excess": o.excess,
                               "max_mult": o.max_mult, "min_mult": o.min_mult, "max_dd": o.max_dd,
                               "d2": o.days_to[2], "d5": o.days_to[5], "d10": o.days_to[10],
                               "perm_loss": o.perm_loss, "entry_day": o.entry_day,
                               "end_day": o.end_day})
    assign_size_quintiles(uni)
    if drops:
        by_reason: dict[str, set] = {}
        for (cik, _, reason) in drops:
            by_reason.setdefault(reason, set()).add(cik)
        log("  companies listed at a cohort date but without a usable price series "
            "(absent from universe and candidates): " +
            ", ".join(f"{r}={len(cs):,}" for r, cs in sorted(by_reason.items())))
        if dropped is not None:
            dropped.extend({"cik": k[0], "ticker": k[1], "reason": k[2], "months": m}
                           for k, m in sorted(drops.items()))
    missing = sorted({r["code"] for r in uni} - {c for c in {r["code"] for r in uni}
                                                  if getattr(store, "has_splits", lambda c: True)(c)})
    if missing:
        log(f"  WARNING: {len(missing):,} codes have no split data, so the price-adjustment "
            f"consistency check was skipped for them (run: market_data.py splits --universe ...)")
    return (pa.Table.from_pylist(uni, schema=UNIVERSE_SCHEMA),
            pa.Table.from_pylist(labels, schema=LABEL_SCHEMA))


def assign_size_quintiles(rows: list[dict]) -> None:
    """size_q 1 (least liquid) .. 5 within each month's eligible names; 0 if ineligible."""
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        if r["eligible"]:
            by_month.setdefault(r["month"], []).append(r)
    for rs in by_month.values():
        order = np.argsort([r["adv60"] for r in rs], kind="stable")
        for rank, idx in enumerate(order):
            rs[idx]["size_q"] = 1 + rank * 5 // len(rs)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--massive", default="data/market/massive")
    p.add_argument("--eodhd", default="data/market/eodhd")
    p.add_argument("--out", default="data/features")
    p.add_argument("--from", dest="first", default="2011-01-01")
    p.add_argument("--to", dest="last", default="2024-12-01")
    a = p.parse_args(argv)
    from market_data import load_price_index

    master = SecurityMaster.load(Path(a.massive), load_price_index(Path(a.eodhd)),
                                symbols=Path(a.eodhd) / "symbols.parquet")
    store = px.PriceStore(Path(a.eodhd))
    months = month_starts(a.first, a.last)
    dropped: list[dict] = []
    uni, labels = build(master, store, months, dropped=dropped)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(uni, out / "universe.parquet", compression="zstd")
    pq.write_table(labels, out / "labels.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(dropped, schema=DROPPED_SCHEMA),
                   out / "universe_dropped.parquet", compression="zstd")
    print(f"{uni.num_rows:,} universe rows, {labels.num_rows:,} labels -> {out}; "
          f"{len({d['cik'] for d in dropped}):,} companies without a price series listed in "
          f"universe_dropped.parquet; price data ends {px.to_iso(store.data_end)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
