"""Is the universe's dollar volume distorted by splits made *after* the cohort date? Read only.

    python scripts/volume_check.py [--tickers AMZN JPM BKNG] [--month 2020-07-01]

Found 2026-10-27 while checking the blue-chip filter: AMZN showed $207B of daily dollar volume
in July 2020 (about $10-15B in fact) and the five "most traded" names were the five that split
most afterwards. ``prices.trailing_dollar_volume`` multiplies the *raw* close by EODHD's volume,
and that volume is split-adjusted to the download date: a later 20:1 split makes the volume
20 times too large, a later 1:20 reverse split 20 times too small. Since winners split and dying
companies reverse-split, that is look-ahead in ``adv60``, so in the eligibility floor and the
liquidity quintiles (size_q).

This script measures it from what is on disk, without rebuilding anything: the corrected volume
is ``adv60`` times the product of 1/ratio over the code's splits after the cohort date (the same
factor ``prices.inconsistent_adjustment_days`` uses). It prints how many company-months move,
which way, and the 5-year outcomes of the rows whose eligibility the correction flips, so the
size of the bias on the universe's base rates is visible before deciding on a rebuild.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from universe import MIN_ADV  # noqa: E402


def split_factors(splits_dir: Path, codes: set[str]) -> tuple[dict[str, list[tuple[str, float]]], int]:
    """code -> [(date, ratio)], and the number of codes with no split file."""
    out, missing = {}, 0
    for code in codes:
        path = splits_dir / (quote(code, safe="") + ".parquet")
        if not path.exists():
            missing += 1
            continue
        t = pq.read_table(path).to_pydict()
        out[code] = [(str(d)[:10], float(r)) for d, r in zip(t["date"], t["ratio"]) if r]
    return out, missing


def factor(splits: list[tuple[str, float]], month: str) -> float:
    f = 1.0
    for d, r in splits:
        if d > month and r > 0:
            f /= r
    return f


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--features", default="data/features")
    p.add_argument("--market", default="data/market/eodhd")
    p.add_argument("--tickers", nargs="*", default=["AMZN", "NVDA", "BKNG", "JPM", "AAPL", "GME"])
    p.add_argument("--month", default="2020-07-01")
    a = p.parse_args(argv)
    feats = Path(a.features)
    uni = pq.read_table(feats / "universe.parquet",
                        columns=["month", "cik", "ticker", "code", "price", "adv60", "eligible", "size_q"]).to_pylist()
    spl, missing = split_factors(Path(a.market) / "splits", {r["code"] for r in uni if r["code"]})
    print(f"{len(uni):,} universe rows, {len({r['code'] for r in uni}):,} codes; {missing:,} codes with no split file "
          f"(treated as never split)")
    moved = defaultdict(int)
    flips_out, flips_in = [], []
    for r in uni:
        f = factor(spl.get(r["code"], []), r["month"])
        r["f"] = f
        adv = r["adv60"]
        r["adv_fixed"] = None if adv is None else adv * f
        if f > 1.0001:
            moved["reverse split later (volume understated)"] += 1
        elif f < 0.9999:
            moved["split later (volume overstated)"] += 1
        if adv is None or r["price"] is None or r["price"] < 1:
            continue
        now, fixed = adv >= MIN_ADV, adv * f >= MIN_ADV
        if r["eligible"] and not fixed:
            flips_out.append(r)
        elif not r["eligible"] and fixed and now is False:
            flips_in.append(r)
    for k, v in moved.items():
        print(f"  {k}: {v:,} company-months ({v / len(uni):.1%})")
    print(f"eligibility flips: {len(flips_out):,} eligible rows fall below ${MIN_ADV:,.0f} when corrected "
          f"(later splits), {len(flips_in):,} ineligible rows rise above it (later reverse splits)")

    # 5y outcomes of the flipped rows, dev years, against the eligible universe
    import duckdb
    con = duckdb.connect()
    con.register("flip", pa.Table.from_pylist(
        [{"month": r["month"], "cik": r["cik"], "kind": "out"} for r in flips_out]
        + [{"month": r["month"], "cik": r["cik"], "kind": "in"} for r in flips_in],
        schema=pa.schema([("month", pa.string()), ("cik", pa.string()), ("kind", pa.string())])))
    q = f"""select coalesce(f.kind, case when u.eligible then 'eligible now' else 'other' end) grp,
                   substr(l.month, 1, 4) between '2012' and '2018' as dev, count(*) n,
                   avg(case when l.max_mult >= 5 then 1.0 else 0 end) p5,
                   avg(case when l.perm_loss then 1.0 else 0 end) p_loss
            from read_parquet('{feats / 'labels.parquet'}') l
            join read_parquet('{feats / 'universe.parquet'}') u on u.month = l.month and u.cik = l.cik
            left join flip f on f.month = l.month and f.cik = l.cik
            where l.years = 5 and l.status in ('complete', 'delisted')
            group by 1, 2 order by 2 desc, 1"""
    print("\n5y outcomes (complete or delisted labels): group, dev 2012-2018?, n, P(5x), P(permanent loss)")
    for g, dev, n, p5, pl in con.execute(q).fetchall():
        print(f"  {g:14s} {'dev' if dev else 'other':5s} {n:9,d}  {p5:.3f}  {pl:.3f}")

    # the most traded names by month, before and after the correction
    for m in [a.month]:
        rows = [r for r in uni if r["month"] == m and r["eligible"] and r["adv60"] is not None]
        top = sorted(rows, key=lambda r: -r["adv60"])[:10]
        fixed = sorted((r for r in rows if r["adv_fixed"] is not None), key=lambda r: -r["adv_fixed"])[:10]
        print(f"\n{m} most traded as stored: " + ", ".join(f"{r['ticker']} ${r['adv60'] / 1e6:,.0f}M" for r in top))
        print(f"{m} most traded corrected: " + ", ".join(f"{r['ticker']} ${r['adv_fixed'] / 1e6:,.0f}M" for r in fixed))
        for t in a.tickers:
            for r in rows:
                if (r["ticker"] or "").upper() == t.upper():
                    print(f"  {t}: code {r['code']}, price ${r['price']:,.2f}, stored ${r['adv60'] / 1e6:,.1f}M, "
                          f"split factor after {r['f']:.4g}, corrected ${r['adv_fixed'] / 1e6:,.1f}M, size_q {r['size_q']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
