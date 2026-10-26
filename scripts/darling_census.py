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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--features", default="data/features/post_features.parquet")
    p.add_argument("--years", nargs=2, type=int, default=[2012, 2024])
    p.add_argument("--show", type=int, default=8, help="candidates listed per year at the listed level")
    p.add_argument("--list-level", default="C")
    p.add_argument("--tickers", nargs="*", default=["ASML", "RKLB"])
    a = p.parse_args(argv)
    import duckdb
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
