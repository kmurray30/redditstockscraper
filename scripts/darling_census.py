"""How many Reddit darlings are there, year by year, across the whole corpus? Counts only.

    python scripts/darling_census.py [--years 2012 2024] [--show 8] [--tickers ASML RKLB]

No model, no grades, no returns: the question is only where the darlings are, so the user can
decide which years to develop on and whether more data (wallstreetbets, more grading) is worth
paying for. Nothing here reads a price or a label, so counting the 2021-2022 posts does not
consult the sealed holdout.

A **substantive subject post** is the ``substantive`` baseline's rule: a cashtag, bare-ticker
mention the company is the subject of (cashtag, in the title, or named twice), in a post naming
at most two companies, that is a self post of >= 400 characters or a link post with >= 200
characters or a primary-source link. Per company and calendar month, over the 90 days ending
that month: distinct (non-deleted) authors of such posts, the number of posts, and the comments
those posts drew. A company is a **darling candidate** the first month it crosses a level's
author and comment floors (``LEVELS``), then cools down for ``COOLDOWN`` months.

Comment counts are the dumps' final values. That is fine for a census (it counts what the crowd
eventually did); it is not point-in-time and must not become a backtest feature as is. The
corpus also grows a lot after 2020 (StockMarket and ValueInvesting), so later counts rise partly
because there is more Reddit; the per-year total of substantive posts is printed for scale.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from baselines import (BAD_AUTHORS, RELIABLE_METHODS, SUBSTANTIVE_LINK_CHARS,  # noqa: E402
                       SUBSTANTIVE_MAX_COMPANIES, SUBSTANTIVE_SELF_CHARS)

COOLDOWN = 12
WINDOW = 3       # calendar months in the rolling window (about 90 days)
# (name, distinct authors, comments on their substantive posts) in the window
LEVELS = (("A", 2, 50), ("B", 3, 100), ("C", 4, 200), ("D", 5, 400), ("E", 8, 800))
# Blue chips (user decision 2026-10-27: "filter out the blue chips"). We have no market cap, so a
# blue chip is a company among the BLUE_CHIP_TOP most traded eligible stocks (60-day median dollar
# volume) both that month and twelve months before: the persistently huge names (AAPL, MSFT, AMZN,
# ASML, TSM). A stock that only became heavily traded during its own run (RKLB in late 2024, GME in
# January 2021) is not one.
BLUE_CHIP_TOP = 200


def blue_chips(con, universe: Path, top: int = BLUE_CHIP_TOP) -> set[tuple[str, str]]:
    """(month, cik) pairs that are blue chips by the rule above (empty if top <= 0)."""
    if top <= 0 or not universe.exists():
        return set()
    return {(m, c) for m, c in con.execute(f"""
        with r as (select month, cik, rank() over (partition by month order by adv60 desc) rk
                   from read_parquet('{universe}') where eligible and adv60 is not null)
        select a.month, a.cik from r a join r b
          on b.cik = a.cik and cast(b.month as date) = cast(a.month as date) - interval 12 month
        where a.rk <= {top} and b.rk <= {top}""").fetchall()}


def monthly(con, parquet: Path, mentions: Path, features: Path, years: tuple[int, int]) -> list[tuple]:
    """(cik, ticker, month_index, posts, authors, comments) per company and calendar month of
    substantive subject posts (not yet rolled)."""
    meth = ",".join(f"'{m}'" for m in RELIABLE_METHODS)
    bad = ",".join("'" + a.replace("'", "''") + "'" for a in BAD_AUTHORS)
    con.execute(f"""create or replace temp table sp as
        with pm as (
            select post_id, cik, any_value(ticker) ticker, bool_or(method = 'cashtag') cashtag,
                   bool_or(in_title) in_title, sum(n) n
            from read_parquet('{mentions}') where source = 'post' and method in ({meth}) and cik is not null
            group by 1, 2),
        nco as (select post_id, count(*) n_companies from pm group by 1),
        p as (select id, author, is_self, num_comments, created_utc
              from read_parquet('{parquet}/posts/*.parquet'))
        select pm.cik, pm.ticker, p.author, p.num_comments,
               year(to_timestamp(p.created_utc)) * 12 + month(to_timestamp(p.created_utc)) - 1 as mi
        from pm join nco using (post_id) join p on p.id = pm.post_id
        join read_parquet('{features}') f on f.post_id = pm.post_id
        where (pm.cashtag or pm.in_title or pm.n >= 2)
          and nco.n_companies <= {SUBSTANTIVE_MAX_COMPANIES}
          and ((p.is_self and f.f_chars >= {SUBSTANTIVE_SELF_CHARS})
               or (not p.is_self and (f.f_chars >= {SUBSTANTIVE_LINK_CHARS} or f.f_primary_links > 0)))
          and year(to_timestamp(p.created_utc)) between {years[0] - 1} and {years[1]}""")
    return con.execute(f"""select cik, any_value(ticker), mi, count(*) as n_posts,
                                  count(distinct case when coalesce(author, '') not in ({bad}) then author end) as n_auth,
                                  sum(coalesce(num_comments, 0)) as n_comm
                           from sp group by cik, mi""").fetchall()


def rolled(rows: list[tuple], con) -> dict[str, list[tuple]]:
    """cik -> [(month_index, posts, authors, comments)] over the WINDOW months ending there.
    Authors are distinct over the whole window, not summed per month."""
    bad = ",".join("'" + a.replace("'", "''") + "'" for a in BAD_AUTHORS)
    out = defaultdict(list)
    for cik, mi, posts, authors, comments in con.execute(f"""
            with months as (select distinct cik, mi from sp)
            select m.cik, m.mi, count(*) as n_posts,
                   count(distinct case when coalesce(s.author, '') not in ({bad}) then s.author end) as n_auth,
                   sum(coalesce(s.num_comments, 0)) as n_comm
            from months m join sp s on s.cik = m.cik and s.mi between m.mi - {WINDOW - 1} and m.mi
            group by 1, 2 order by 1, 2""").fetchall():
        out[cik].append((int(mi), int(posts), int(authors), int(comments)))
    return out


def candidates(roll: dict, tickers: dict, authors_min: int, comments_min: int, years) -> list[dict]:
    out = []
    for cik, series in roll.items():
        last = None
        for mi, posts, authors, comments in series:
            y = mi // 12
            if not years[0] <= y <= years[1] or authors < authors_min or comments < comments_min:
                continue
            if last is not None and mi - last < COOLDOWN:
                continue
            last = mi
            out.append({"cik": cik, "ticker": tickers.get(cik), "month": f"{y}-{mi % 12 + 1:02d}",
                        "posts": posts, "authors": authors, "comments": comments})
    return sorted(out, key=lambda e: (e["month"], -e["comments"]))


def chip_report(con, universe: Path, months: list[str], tickers: list[str], top: int = BLUE_CHIP_TOP, log=print):
    """For a check by eye: the most traded eligible stocks in each month, and the dollar-volume
    rank of each ticker that month and twelve months before."""
    con.execute(f"""create or replace temp table rk as
        select month, cik, ticker, adv60, rank() over (partition by month order by adv60 desc) rk,
               count(*) over (partition by month) n
        from read_parquet('{universe}') where eligible and adv60 is not null""")
    for m in months:
        rows = con.execute("select ticker, adv60, n from rk where month = ? order by rk limit 12", [m]).fetchall()
        log(f"\n{m}: {rows[0][2] if rows else 0:,} eligible; most traded: "
            + ", ".join(f"{t} ${a / 1e6:,.0f}M" for t, a, _ in rows))
        for t in tickers:
            got = con.execute("""select a.ticker, a.rk, a.adv60, b.rk from rk a left join rk b on b.cik = a.cik
                                   and cast(b.month as date) = cast(a.month as date) - interval 12 month
                                 where a.month = ? and upper(a.ticker) = ?""", [m, t.upper()]).fetchall()
            log(f"  {t}: " + ("; ".join(f"rank {r} (${a / 1e6:,.1f}M), a year before {rb}" for _, r, a, rb in got)
                              or "not eligible that month"))


def write_candidates(roll: dict, level: str, years, universe: Path, out: Path, con, log=print,
                     blue_chip_top: int = 0) -> int:
    """Every company-month at or above ``level`` (no cooldown, so a long darling run is graded at
    each cutoff), mapped to the cutoff that closes the window (the first of the next month) and
    kept only where the company is eligible in the universe then: that drops ETFs and funds.
    With ``blue_chip_top`` the blue chips at that cutoff are dropped too (nothing is paid for them)."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    _, au, cm = next(lv for lv in LEVELS if lv[0] == level)
    rows = []
    for cik, series in roll.items():
        for mi, posts, authors, comments in series:
            if years[0] <= mi // 12 <= years[1] and authors >= au and comments >= cm:
                nxt = mi + 1
                rows.append({"month": f"{nxt // 12}-{nxt % 12 + 1:02d}-01", "cik": cik})
    eligible = set()
    if universe.exists() and rows:
        con.register("cand", pa.Table.from_pylist(rows))
        eligible = {(m, c) for m, c in con.execute(
            f"select c.month, c.cik from cand c join read_parquet('{universe}') u "
            f"on u.month = c.month and u.cik = c.cik where u.eligible").fetchall()}
    kept = [r for r in rows if (r["month"], r["cik"]) in eligible]
    chips = blue_chips(con, universe, blue_chip_top)
    n_chips = sum((r["month"], r["cik"]) in chips for r in kept)
    kept = [r for r in kept if (r["month"], r["cik"]) not in chips]
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(kept, schema=pa.schema([("month", pa.string()), ("cik", pa.string())])), out)
    log(f"\nwrote {len(kept):,} grading candidates (level {level}, {len({r['cik'] for r in kept}):,} companies; "
        f"{len(rows) - len(kept) - n_chips:,} company-months dropped as not eligible: funds, ETFs, delisted"
        + (f"; {n_chips:,} as blue chips, top {blue_chip_top} by dollar volume now and a year before" if blue_chip_top > 0 else "")
        + f") to {out}")
    return len(kept)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--features", default="data/features/post_features.parquet")
    p.add_argument("--years", nargs=2, type=int, default=[2012, 2024])
    p.add_argument("--show", type=int, default=8, help="candidates listed per year at the listed level")
    p.add_argument("--list-level", default="C")
    p.add_argument("--tickers", nargs="*", default=["ASML", "RKLB"])
    p.add_argument("--write", help="write every company-month crossing --write-level (no cooldown) as grading "
                                   "candidates: parquet of (month = the cutoff after the window, cik)")
    p.add_argument("--write-level", default="C")
    p.add_argument("--universe", default="data/features/universe.parquet",
                   help="with --write: keep only companies eligible at that cutoff (drops ETFs and funds)")
    p.add_argument("--blue-chip-top", type=int, default=0,
                   help=f"with --write: drop blue chips (top N by dollar volume now and a year before; "
                        f"{BLUE_CHIP_TOP} is the darling study's choice, 0 = keep them)")
    p.add_argument("--chip-report", nargs="*", metavar="MONTH",
                   help="print the dollar-volume ranks for these months (YYYY-MM-01) and --tickers, then stop")
    a = p.parse_args(argv)
    import duckdb
    if a.chip_report:
        chip_report(duckdb.connect(), Path(a.universe), a.chip_report, a.tickers)
        return 0
    years = (a.years[0], a.years[1])
    con = duckdb.connect()
    rows = monthly(con, Path(a.parquet), Path(a.mentions), Path(a.features), years)
    tickers = {r[0]: r[1] for r in rows}
    roll = rolled(rows, con)
    per_year_posts = Counter()
    for _, _, mi, posts, _, _ in rows:
        if years[0] <= mi // 12 <= years[1]:
            per_year_posts[mi // 12] += posts
    yrs = list(range(years[0], years[1] + 1))
    print("substantive subject posts per year (scale): " + " ".join(f"{y}:{per_year_posts[y]:,}" for y in yrs))
    print(f"\ndarling candidates per year (first month a company crosses the floors over {WINDOW} months, "
          f"{COOLDOWN}-month cooldown):")
    print("  level  authors  comments  " + " ".join(f"{y:>5}" for y in yrs) + "  total")
    found = {}
    for name, au, cm in LEVELS:
        ev = candidates(roll, tickers, au, cm, years)
        found[name] = ev
        c = Counter(int(e["month"][:4]) for e in ev)
        print(f"  {name:5s}  >= {au:<5d} >= {cm:<6d}  " + " ".join(f"{c.get(y, 0):5d}" for y in yrs) + f"  {len(ev):5d}")
    lv = a.list_level
    print(f"\nlevel {lv} candidates by year (most-commented first; ticker, month, authors, posts, comments):")
    by_year = defaultdict(list)
    for e in found.get(lv, []):
        by_year[int(e["month"][:4])].append(e)
    for y in yrs:
        evs = sorted(by_year.get(y, []), key=lambda e: -e["comments"])
        print(f"  {y} ({len(evs)}): " + ", ".join(f"{e['ticker']} {e['month'][5:]} {e['authors']}a/{e['posts']}p/{e['comments']}c"
                                             for e in evs[:a.show]))
    if a.write:
        write_candidates(roll, a.write_level, years, Path(a.universe), Path(a.write), con,
                         blue_chip_top=a.blue_chip_top)
    for t in a.tickers:
        ciks = [c for c, tk in tickers.items() if (tk or "").upper() == t.upper()]
        print(f"\n-- {t}: peak 90-day window per year (authors / posts / comments)")
        for cik in ciks:
            best = {}
            for mi, posts, authors, comments in roll.get(cik, []):
                y = mi // 12
                if years[0] <= y <= years[1] and (y not in best or (authors, comments) > best[y][1:3:]):
                    best[y] = (posts, authors, comments)
            print("   " + (", ".join(f"{y} {au}a/{po}p/{cm}c" for y, (po, au, cm) in sorted(best.items()))
                           or "no substantive subject posts"))
        if not ciks:
            print("   no substantive subject posts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
