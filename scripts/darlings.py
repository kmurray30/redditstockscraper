"""Reddit darlings: the rare companies a few informed posters made a careful bull case for and
independent commenters backed with their own evidence. Everything else is ignored.

    python scripts/darlings.py run [--prompt thesis_v2] [--variant identified] [--split dev]
                                   [--inspect ASML RKLB]

User decision 2026-10-26: the strategy is not to rank every company every month but to act only
on "Reddit darlings" (RKLB a couple of years ago, ASML): an informed post (argument and evidence)
is what counts first, independent commenters affirming it with their own reasoning is how the
poster's bias is checked, and a lot of discussion adds weight. Negative sentiment matters only as
a counterweight; missing a winner that drew heavy, intelligent opposition is acceptable (false
negatives are fine). The user wants at least five darlings per year.

Built from the thesis grades already paid for (``thesis_threads_<variant>[.<prompt>].parquet``),
so it costs nothing. Two steps, deliberately in this order:

1. **Count, no returns.** A ladder of thread rules (``LEVELS``, loosest first) is applied; a
   company becomes a darling the first month a qualifying thread about it is visible, then cools
   down for ``COOLDOWN`` months. Per level the job prints darlings per year. The level used is the
   strictest one that gives at least ``MIN_PER_YEAR`` darlings in every year of the split: a
   count rule, never a return rule.
2. **Measure, once.** For that level, with and without the opposition rule, every darling's
   1/3/5-year outcome (entry at the open after the month's cutoff, the backtest's labels) against
   the S&P 500 (excess return), against every eligible company that month, and against a matched
   control: eligible companies the same month in the same liquidity quintile and the same prior
   12-1 momentum quintile. The matched control is the bull-market check: darlings are growth and
   momentum names, so they are compared with stocks that had run just as hard. Rates are given
   per year too, so an effect that only exists in some years shows.

For "recent" the threads are only those of the darling candidates (``darling_census.py --write``:
company-months with heavy discussion by several authors), planned by ``thesis_funnel.py --only``
and graded with thesis_v2 into ``--grades-dir`` (``data/features/darlings``), so the dev grades are
untouched. Outcomes from 2023-2024 are partly censored: only horizons that have ended count.

Point in time: a thread counts from its first monthly cutoff (``first_month``), which is when the
backtest could first see it; the grades themselves follow the thesis pipeline's no-lock view (the
model saw every comment the thread ever got), and ``late_shown`` darlings are counted separately.
Only the development split has grades; the holdout is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from thesis_prompt import prompt_name  # noqa: E402
from thesis_scorer import load_grades  # noqa: E402

COOLDOWN = 12          # months before the same company can be a new darling
MIN_PER_YEAR = 5       # the user's floor
LOOKBACK = 3           # months of qualifying threads counted as "the case" at an event
# "recent" (user decision 2026-10-27): the darling study develops on 2019-2024, where the darlings
# are; that uses up the sealed 2021-2022 holdout, which is recorded in the ledger when it runs.
SPLIT_YEARS = {"dev": (2012, 2018), "validate": (2019, 2020), "recent": (2019, 2024)}
HORIZONS = (1, 3, 5)
FINAL = ("complete", "delisted")


@dataclass(frozen=True)
class Level:
    """One thread rule. Grades are 0-100 (Jev's five levels x25, probability-weighted)."""
    name: str
    argument: float        # the post's reasoning
    evidence: float        # the post's sourcing
    support: float         # independent comments adding reasoning or evidence in favour
    voices: float          # distinct substantive commenters
    comments: int          # discussion volume
    # the opposition rule (the "strict" variant): no strong independent case against
    max_opposition: float = 50.0
    max_objection: float = 75.0


LEVELS = (
    Level("L1 loose", argument=50, evidence=25, support=25, voices=1, comments=5),
    Level("L2", argument=50, evidence=50, support=50, voices=2, comments=10),
    Level("L3", argument=60, evidence=50, support=50, voices=2, comments=15),
    Level("L4", argument=75, evidence=50, support=60, voices=3, comments=20),
    Level("L5 strict", argument=75, evidence=75, support=75, voices=3, comments=25),
)


def bullish(g: dict) -> bool:
    """The poster argues for the company and the thread, comments included, still points up.
    ``direction_5`` is thesis_v2's own net direction; v1 grades fall back on the stance."""
    if (g.get("stance") or 0) < 0.25:
        return False
    d = g.get("direction_5")
    return d is None or d >= 4


def failures(g: dict, lv: Level, strict: bool) -> list[str]:
    """Why a thread is not a darling thread at this level (empty = it is one)."""
    out = []
    if not bullish(g):
        out.append(f"not bullish (stance {float(g.get('stance') or 0):+.2f}, direction {g.get('direction_5')})")
    for key, field, lim in (("argument", "argument_quality", lv.argument), ("evidence", "evidence_quality", lv.evidence),
                            ("support", "independent_support", lv.support), ("voices", "n_independent_voices", lv.voices),
                            ("comments", "n_comments", lv.comments)):
        v = float(g.get(field) or 0)
        if v < lim:
            out.append(f"{key} {v:.0f} < {lim:g}")
    if strict:
        opp, obj = float(g.get("independent_opposition") or 0), float(g.get("strongest_objection_severity") or 0)
        if opp >= lv.max_opposition:
            out.append(f"opposition {opp:.0f} >= {lv.max_opposition:g}")
        if obj >= lv.max_objection:
            out.append(f"objection {obj:.0f} >= {lv.max_objection:g}")
    return out


def month_index(m: str) -> int:
    return int(m[:4]) * 12 + int(m[5:7]) - 1


def events(grades: list[dict], lv: Level, strict: bool, years: tuple[int, int]) -> list[dict]:
    """One row per darling event: the company, the month it first qualified (after any
    cooldown), and the qualifying threads visible in the ``LOOKBACK`` months up to then."""
    hits = [g for g in grades if not failures(g, lv, strict)
            and years[0] <= int(g["first_month"][:4]) <= years[1]]
    by_cik: dict[str, list[dict]] = defaultdict(list)
    for g in hits:
        by_cik[g["cik"]].append(g)
    out = []
    for cik, gs in by_cik.items():
        gs.sort(key=lambda g: (g["first_month"], g["post_id"]))
        last = None
        for g in gs:
            mi = month_index(g["first_month"])
            if last is not None and mi - last < COOLDOWN:
                continue
            last = mi
            case = [x for x in gs if 0 <= mi - month_index(x["first_month"]) < LOOKBACK]
            out.append({"cik": cik, "ticker": g.get("ticker"), "name": g.get("name"), "month": g["first_month"],
                        "threads": [x["post_id"] for x in case], "n_threads": len(case),
                        "authors": len({x.get("author") for x in case}),
                        "comments": int(sum(x.get("n_comments") or 0 for x in case)),
                        "late": any((x.get("late_shown") or 0) > 0 or x.get("edited_late") for x in case)})
    out.sort(key=lambda e: (e["month"], e["ticker"] or ""))
    return out


def per_year(evs: list[dict], years: tuple[int, int]) -> dict[int, int]:
    c = Counter(int(e["month"][:4]) for e in evs)
    return {y: c.get(y, 0) for y in range(years[0], years[1] + 1)}


def pick_level(grades, years) -> tuple[Level, dict]:
    """The strictest level (opposition rule on) with at least MIN_PER_YEAR darlings every year;
    the loosest if none reaches it. Counts only: no outcome is read here."""
    table, chosen = {}, None
    for lv in LEVELS:
        table[lv.name] = {"strict": per_year(events(grades, lv, True, years), years),
                          "lenient": per_year(events(grades, lv, False, years), years)}
        if min(table[lv.name]["strict"].values()) >= MIN_PER_YEAR:
            chosen = lv
    return chosen or LEVELS[0], table


# ------------------------------------------------------------------ outcomes

def open_db(features: Path):
    import duckdb
    con = duckdb.connect()
    con.execute(f"create view universe as select * from read_parquet('{features / 'universe.parquet'}')")
    con.execute(f"create view labels as select * from read_parquet('{features / 'labels.parquet'}')")
    return con


def outcome_tables(con, months: list[str]) -> tuple[dict, dict, dict]:
    """(month, cik, years) -> outcome row with its matched cell; and per (month, size_q, mom_q,
    years) and per (month, years) the rates of the eligible universe."""
    marks = ",".join("?" * len(months))
    con.execute(f"""create or replace temp table u as
        select month, cik, size_q,
               case when mom is null or isnan(mom) then 0
                    else ntile(5) over (partition by month, (mom is null or isnan(mom)) order by mom) end as mom_q
        from universe where eligible and month in ({marks})""", months)
    con.execute("""create or replace temp table ul as
        select u.month, u.cik, u.size_q, u.mom_q, l.years, l.status, l.excess, l.max_mult, l.perm_loss, l.cagr
        from u join labels l on l.month = u.month and l.cik = u.cik""")
    rows = {}
    for r in con.execute("select * from ul").fetchall():
        rows[(r[0], r[1], int(r[4]))] = {"size_q": r[2], "mom_q": r[3], "status": r[5], "excess": r[6],
                                         "max_mult": r[7], "perm_loss": r[8], "cagr": r[9]}
    agg = """count(*) n, avg(case when excess > 0 then 1.0 else 0 end) p_beat, median(excess) med_excess,
             avg(excess) mean_excess, avg(case when max_mult >= 2 then 1.0 else 0 end) p2,
             avg(case when max_mult >= 5 then 1.0 else 0 end) p5, avg(case when perm_loss then 1.0 else 0 end) p_loss"""
    final = "status in ('complete', 'delisted') and excess is not null and not isnan(excess)"
    cols = ["n", "p_beat", "med_excess", "mean_excess", "p2", "p5", "p_loss"]
    cells = {(r[0], r[1], r[2], int(r[3])): dict(zip(cols, r[4:])) for r in con.execute(
        f"select month, size_q, mom_q, years, {agg} from ul where {final} group by 1, 2, 3, 4").fetchall()}
    months_ = {(r[0], int(r[1])): dict(zip(cols, r[2:])) for r in con.execute(
        f"select month, years, {agg} from ul where {final} group by 1, 2").fetchall()}
    return rows, cells, months_


def measure(evs: list[dict], rows, cells, months_, years: int) -> dict:
    """Darling rates at one horizon, next to the same-month universe and the matched control
    (the average of each darling's own cell), with a bootstrap interval on the beat-rate gap."""
    got, uni, mat = [], [], []
    for e in evs:
        r = rows.get((e["month"], e["cik"], years))
        if not r or r["status"] not in FINAL or r["excess"] is None or not np.isfinite(r["excess"]):
            continue
        cell = cells.get((e["month"], r["size_q"], r["mom_q"], years))
        mon = months_.get((e["month"], years))
        if not cell or not mon:
            continue
        got.append(r)
        uni.append(mon)
        mat.append(cell)
    n = len(got)
    if n == 0:
        return {"n": 0}
    ex = np.array([r["excess"] for r in got], float)
    beat = (ex > 0).astype(float)
    m_beat = np.array([c["p_beat"] for c in mat], float)
    rng = np.random.default_rng(0)
    gaps = [float((beat[i] - m_beat[i]).mean()) for i in (rng.integers(0, n, n) for _ in range(2000))]
    return {"n": n,
            "p_beat": float(beat.mean()), "med_excess": float(np.median(ex)), "mean_excess": float(ex.mean()),
            "p2": float(np.mean([r["max_mult"] >= 2 for r in got])), "p5": float(np.mean([r["max_mult"] >= 5 for r in got])),
            "p_loss": float(np.mean([bool(r["perm_loss"]) for r in got])),
            "universe": {k: float(np.mean([u[k] for u in uni])) for k in ("p_beat", "med_excess", "p2", "p5", "p_loss")},
            "matched": {k: float(np.mean([c[k] for c in mat])) for k in ("p_beat", "med_excess", "p2", "p5", "p_loss")},
            "beat_gap_ci": [float(np.percentile(gaps, 2.5)), float(np.percentile(gaps, 97.5))]}


def pct(x) -> str:
    return "  n/a" if x is None or not np.isfinite(x) else f"{x * 100:5.1f}%"


def report(label: str, evs: list[dict], rows, cells, months_, log=print) -> dict:
    log(f"\n== {label}: {len(evs)} darlings, {len({e['cik'] for e in evs})} companies")
    out = {}
    for y in HORIZONS:
        m = measure(evs, rows, cells, months_, y)
        out[y] = m
        if not m["n"]:
            log(f"  {y}y: no final outcomes")
            continue
        u, c = m["universe"], m["matched"]
        log(f"  {y}y  n {m['n']:3d}  beat market {pct(m['p_beat'])} (matched {pct(c['p_beat'])}, all {pct(u['p_beat'])}; "
            f"gap 95% CI {m['beat_gap_ci'][0] * 100:+.0f} to {m['beat_gap_ci'][1] * 100:+.0f} pts)  "
            f"median excess {pct(m['med_excess'])} (matched {pct(c['med_excess'])})  mean excess {pct(m['mean_excess'])}  "
            f"2x {pct(m['p2'])} ({pct(c['p2'])})  5x {pct(m['p5'])} ({pct(c['p5'])})  "
            f"perm loss {pct(m['p_loss'])} ({pct(c['p_loss'])})")
    by_year = defaultdict(list)
    for e in evs:
        by_year[int(e["month"][:4])].append(e)
    log("  by year (3y horizon): year  n  beat market (matched)  median excess (matched)")
    for yr in sorted(by_year):
        m = measure(by_year[yr], rows, cells, months_, 3)
        if m["n"]:
            log(f"    {yr}  {m['n']:2d}  {pct(m['p_beat'])} ({pct(m['matched']['p_beat'])})  "
                f"{pct(m['med_excess'])} ({pct(m['matched']['med_excess'])})")
    return out


def load_titles(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    import zstandard as zstd
    data = json.loads(zstd.ZstdDecompressor().decompress(path.read_bytes(), max_output_size=2_000_000_000))
    return {r["post_id"]: r.get("title") or "" for r in data}


def corpus_titles(parquet: Path, post_ids: list[str]) -> dict[str, str]:
    files = list((parquet / "posts").glob("*.parquet"))
    if not files or not post_ids:
        return {}
    import duckdb
    import pyarrow as pa
    con = duckdb.connect()
    con.register("want", pa.table({"id": pa.array(post_ids)}))
    return dict(con.execute(f"select p.id, p.title from read_parquet('{parquet}/posts/*.parquet') p "
                            "join want w on w.id = p.id").fetchall())


def mark_holdout_used(note: str) -> None:
    """The user gave up the sealed 2021-2022 holdout for the darling study (2026-10-27). Record it
    in the ledger's holdout state, so any later one-look evaluation of 2021-2022 knows the period
    has been seen and is refused without --accept-contaminated."""
    import ledger as lg
    from datetime import datetime, timezone
    led = lg.Ledger()
    state = led.holdout_state()
    if any(c.get("config_id") == note for c in state["consulted"]):
        return
    led.root.mkdir(parents=True, exist_ok=True)
    state["consulted"].append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "config_id": note,
                               "why": "user decision 2026-10-27: develop the darling study on 2019-2024"})
    state["contaminated"] = len(state["consulted"]) > 1
    led.holdout_path.write_text(json.dumps(state, indent=2))
    print(f"[holdout] 2021-2022 recorded as seen ({note}); the sealed holdout no longer exists")


def inspect(grades: list[dict], tickers: list[str], lv: Level, titles: dict, mentions: Path, log=print):
    """Every graded thread about each ticker and why it is or is not a darling thread; for a
    ticker with no grades (listed later than the graded years), its post counts by year."""
    for t in tickers:
        gs = sorted((g for g in grades if (g.get("ticker") or "").upper() == t.upper()), key=lambda g: g["first_month"])
        log(f"\n-- {t}: {len(gs)} graded threads at {lv.name}")
        for g in gs:
            why = failures(g, lv, True)
            log(f"  {g['first_month'][:7]} {'DARLING' if not why else 'no     '} stance {float(g.get('stance') or 0):+.2f} "
                f"dir {g.get('direction_5')} cred {g.get('credibility_10')} arg {float(g.get('argument_quality') or 0):.0f} "
                f"ev {float(g.get('evidence_quality') or 0):.0f} sup {float(g.get('independent_support') or 0):.0f} "
                f"opp {float(g.get('independent_opposition') or 0):.0f} obj {float(g.get('strongest_objection_severity') or 0):.0f} "
                f"voices {g.get('n_independent_voices')} cmts {g.get('n_comments')} | {titles.get(g['post_id'], '')[:70]}"
                + (f"  [{'; '.join(why)}]" if why else ""))
        if not gs and mentions.exists():
            import duckdb
            rows = duckdb.connect().execute(
                f"""select year(to_timestamp(created_utc)) y, count(distinct post_id) posts,
                           count(distinct case when in_title or method = 'cashtag' then post_id end) subject_posts
                    from read_parquet('{mentions}') where upper(ticker) = ? and source = 'post'
                    group by 1 order by 1""", [t.upper()]).fetchall()
            log("  not in the graded years; posts naming it by year (year, posts, posts with it in the title or as a cashtag): "
                + ", ".join(f"{y} {p}/{s}" for y, p, s in rows) if rows else "  no posts name it")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cmd", choices=["run"])
    p.add_argument("--features", default="data/features")
    p.add_argument("--variant", default="identified")
    p.add_argument("--prompt", default=None, help="grades of this prompt (default THESIS_PROMPT, else thesis_v1)")
    p.add_argument("--split", default="dev", choices=sorted(SPLIT_YEARS))
    p.add_argument("--inspect", nargs="*", default=["ASML", "RKLB"])
    p.add_argument("--titles", default="data/ui/threads.json.zst")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--out", default=None, help="default data/experiments/darlings_<split>_<variant>.json")
    p.add_argument("--grades-dir", default=None, help="where the grades file is (default --features)")
    p.add_argument("--parquet", default="data/corpus/parquet", help="for thread titles not in the browser export")
    a = p.parse_args(argv)
    prompt = a.prompt or prompt_name()
    years = SPLIT_YEARS[a.split]
    grades = load_grades(Path(a.grades_dir or a.features), a.variant, prompt)
    if years[0] <= 2022 and years[1] >= 2021:
        mark_holdout_used(f"darlings:{a.split}:{a.variant}")
    print(f"{len(grades):,} graded threads ({a.variant}, prompt {prompt}); split {a.split} {years[0]}-{years[1]}")

    # 1. counts only
    lv, table = pick_level(grades, years)
    print(f"\ndarlings per year by level (strict = no strong opposition / lenient = opposition ignored); "
          f"floor {MIN_PER_YEAR} per year, cooldown {COOLDOWN} months:")
    for name, t in table.items():
        print(f"  {name:10s} strict  " + " ".join(f"{y}:{n:3d}" for y, n in t["strict"].items())
              + f"   total {sum(t['strict'].values())}")
        print(f"  {'':10s} lenient " + " ".join(f"{y}:{n:3d}" for y, n in t["lenient"].items())
              + f"   total {sum(t['lenient'].values())}")
    print(f"level used: {lv.name} {asdict(lv)} (the strictest with >= {MIN_PER_YEAR} every year"
          + ("" if min(table[lv.name]['strict'].values()) >= MIN_PER_YEAR else "; NONE reaches the floor, loosest used") + ")")
    titles = load_titles(Path(a.titles))
    missing = sorted({g["post_id"] for g in grades} - set(titles))
    if missing:
        titles.update(corpus_titles(Path(a.parquet), missing))
    inspect(grades, a.inspect, lv, titles, Path(a.mentions))

    # 2. outcomes, once, for the chosen level
    strict, lenient = events(grades, lv, True, years), events(grades, lv, False, years)
    con = open_db(Path(a.features))
    months = sorted({e["month"] for e in strict + lenient})
    rows, cells, months_ = outcome_tables(con, months)
    res = {"level": asdict(lv), "counts": table,
           "strict": report(f"{lv.name}, opposition rule on (the user's rule)", strict, rows, cells, months_),
           "lenient": report(f"{lv.name}, opposition ignored", lenient, rows, cells, months_)}
    late = sum(e["late"] for e in strict)
    print(f"\n{late} of {len(strict)} strict darlings rest on a thread holding content written after its first cutoff")
    print(f"\nthe darlings (opposition rule on), with 1y, 3y and 5y outcomes (excess vs S&P, best multiple; "
          f"n/a = the horizon has not ended):")
    for e in strict:
        o1, o3, o5 = (rows.get((e["month"], e["cik"], y)) or {} for y in HORIZONS)
        def fmt(o):
            if not o or o.get("status") not in FINAL or o.get("excess") is None:
                return "  n/a        "
            return f"{o['excess'] * 100:+6.0f}% x{float(o['max_mult'] or 0):4.1f}"
        first = titles.get(e["threads"][-1], "") if e["threads"] else ""
        print(f"  {e['month'][:7]} {e['ticker'] or '?':6s} {(e['name'] or '')[:24]:24s} threads {e['n_threads']} "
              f"cmts {e['comments']:4d}  1y {fmt(o1)}  3y {fmt(o3)}  5y {fmt(o5)}  | {first[:60]}")
    out = Path(a.out or f"data/experiments/darlings_{a.split}_{a.variant}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    res["events"] = {"strict": strict, "lenient": lenient}
    out.write_text(json.dumps(res, default=str, indent=1))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
