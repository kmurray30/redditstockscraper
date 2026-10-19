"""How long after a post do its comments arrive? Decides the maturation window.

    python scripts/comment_lag.py [--parquet data/corpus/parquet] [--years 2012 2018]

Prints, per year, the share of comments (and of posts' comment counts) that fall within
1h, 6h, 24h, 48h, 7d, 30d and 180d of the post, and the share of posts with at least one
comment inside each window. Reads only ``created_utc`` columns; no text.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

WINDOWS = [("1h", 3600), ("6h", 6 * 3600), ("24h", 86400), ("48h", 2 * 86400),
           ("7d", 7 * 86400), ("30d", 30 * 86400), ("180d", 180 * 86400)]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--years", nargs=2, type=int, default=[2012, 2018])
    a = p.parse_args(argv)
    root = Path(a.parquet)
    con = duckdb.connect()
    con.execute(f"create view posts as select id, created_utc from read_parquet('{root}/posts/*.parquet')")
    con.execute(f"create view comments as select post_id, created_utc from read_parquet('{root}/comments/*.parquet')")
    lo, hi = a.years
    cols = ", ".join(f"avg(case when lag <= {s} then 1 else 0 end) as c_{n}" for n, s in WINDOWS)
    rows = con.execute(f"""
        with j as (
          select year(to_timestamp(p.created_utc)) as y, c.created_utc - p.created_utc as lag
          from comments c join posts p on p.id = c.post_id
          where year(to_timestamp(p.created_utc)) between {lo} and {hi} and c.created_utc >= p.created_utc)
        select y, count(*) as n, {cols} from j group by y order by y""").fetchall()
    print("share of comments arriving within each window of the post")
    print("year        n   " + "  ".join(f"{n:>5s}" for n, _ in WINDOWS))
    for r in rows:
        print(f"{r[0]}  {r[1]:9,d}  " + "  ".join(f"{v:5.1%}" for v in r[2:]))
    cols = ", ".join(
        f"avg(case when first_{n} then 1 else 0 end) as p_{n}" for n, _ in WINDOWS)
    firsts = ", ".join(f"bool_or(c.created_utc - p.created_utc <= {s}) as first_{n}" for n, s in WINDOWS)
    rows = con.execute(f"""
        with j as (
          select p.id, year(to_timestamp(p.created_utc)) as y, {firsts}
          from posts p left join comments c on p.id = c.post_id
          where year(to_timestamp(p.created_utc)) between {lo} and {hi}
          group by p.id, p.created_utc)
        select y, count(*) as n, {cols} from j group by y order by y""").fetchall()
    print("\nshare of posts with at least one comment inside the window")
    print("year        n   " + "  ".join(f"{n:>5s}" for n, _ in WINDOWS))
    for r in rows:
        print(f"{r[0]}  {r[1]:9,d}  " + "  ".join(f"{v:5.1%}" for v in r[2:]))
    med = con.execute(f"""
        with j as (
          select c.created_utc - p.created_utc as lag
          from comments c join posts p on p.id = c.post_id
          where year(to_timestamp(p.created_utc)) between {lo} and {hi} and c.created_utc >= p.created_utc)
        select quantile_cont(lag, [0.5, 0.9, 0.95, 0.99]) from j""").fetchone()[0]
    print("\ncomment lag quantiles (hours): p50 %.1f  p90 %.1f  p95 %.1f  p99 %.1f"
          % tuple(v / 3600 for v in med))
    return 0


if __name__ == "__main__":
    sys.exit(main())
