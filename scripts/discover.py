#!/usr/bin/env python3
"""Look for simple, durable heuristics: classify company-months, compare each class with matched
peers, and keep only what holds in every period. Read only; no model, no spend.

    python scripts/discover.py [--out data/experiments/discover_v1.json]

Pre-registration (written 2026-11-12, before any number below was computed; the class list,
thresholds, periods and decision rule are fixed in this file and its git history):

* Unit: an eligible company-month of the universe (2012-01 .. 2025-09 with outcomes). Features
  use only what was visible at the cohort cutoff, on the baselines' strict view: posts created in
  the 90 days before the cutoff whose 24h maturation window has closed, not edited after it, with
  a reliable mention (cashtag, clean or contextual bare ticker) in a post naming <= 10 companies;
  comments counted only inside each post's first 24 hours. No votes, no late comments, no model.
  "Substantive" is the `substantive` baseline's rule without the deleted-author rule (the subject
  of the post, <= 2 companies, >= 400 characters of self text or a link post with >= 200
  characters or a primary source). Counts are compared within a month (ranks), never across
  years, because the corpus grows about tenfold after 2020.
* Comparison: each company-month against the eligible universe in its own cell, month x
  liquidity quintile x 12-1 momentum quintile (as darlings.py). Gap = class rate minus the mean
  of the members' own cell rates, so market regime, size and momentum are taken out.
* Metrics at horizon h: beat = excess return over the S&P > 0 (primary); big = best multiple
  >= 2 (1y), 3 (3y) or 5 (5y); loss = permanent loss (<= 20% of entry, final). Outcomes that are
  censored or bad data are dropped, never counted as failures.
* Periods: discovery D = cohorts 2012-2018 at 3y (5y and 1y shown); confirmation C1 = 2019-2022
  at 3y; C2 = 2023-01 .. 2025-09 at 1y. C1 includes the validate split and the given-up holdout,
  both already seen by other scorers but never by these classes.
* Uncertainty: cluster bootstrap over companies (Poisson weights, 1,000 draws), because one
  company sits in many consecutive months.
* Decision, per class and metric, decided on the primary metric (beat):
  SURVIVES  the D 95% interval excludes 0, C1 and C2 have D's sign, and the one-sided confirmation
            test (Stouffer of C1 and C2 z in D's direction) passes Benjamini-Hochberg at q = 0.10
            across all classes;
  CONSISTENT  same sign in all three periods but not significant;
  FAILS     otherwise. A negative survivor is a heuristic too (a filter: avoid these).
  The secondary metrics (big, loss) get the same verdict for description, not for the decision.
* Every class is reported, whatever its result; the list below is the whole list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DAY = 86400
WINDOW_DAYS = 90
PRIOR_DAYS = 365
MATURE = DAY
RELIABLE = ("cashtag", "bare_clean", "bare_context")
BAD_AUTHORS = ("[deleted]", "AutoModerator", "None", "")
BIG = {1: 2.0, 3: 3.0, 5: 5.0}
N_BOOT = 1000
Q_BH = 0.10

PERIODS = {  # name -> (first cohort, last cohort, decision horizon, other horizons shown)
    "D": ("2012-01-01", "2018-12-01", 3, (1, 5)),
    "C1": ("2019-01-01", "2022-12-01", 3, (1,)),
    "C2": ("2023-01-01", "2025-09-01", 1, ()),
}

# ------------------------------------------------------------------ post text classes

FUND = re.compile(r"\b(revenues?|sales growth|earnings|eps|margins?|gross profit|operating income|net income"
                  r"|free cash flow|fcf|cash flows?|balance sheet|debt|leverage|valuation|p/?e|ev/ebitda|ebitda"
                  r"|guidance|10-?k|10-?q|annual report|dividends?|buybacks?|moat|market share|backlog"
                  r"|book value|roic|roe|intrinsic value|dcf|discounted cash|tam|customers|competitors?"
                  r"|management|insiders?)\b")
SPEC = re.compile(r"(\b(options|strike price|expiry|expiration|short squeeze|squeeze|short interest|moon(ing)?"
                  r"|rocket|tendies|yolo|chart|technicals?|support|resistance|breakout|rsi|macd|pump(ing|ed)?"
                  r"|hype|fomo|bagholders?|gamma|swing trad\w*|day trad\w*|lambo|diamond hands)\b|\U0001F680)")
QUESTION = re.compile(r"^(thoughts|opinions?|should i|what do you|anyone|is (it|now|this)|why (is|did)"
                      r"|how (is|do)|help|advice|eli5)\b")

PCLASS_SCHEMA = pa.schema([("post_id", pa.string()), ("fund_n", pa.int32()), ("spec_n", pa.int32()),
                           ("is_question", pa.bool_())])


def post_class(title: str | None, selftext: str | None) -> dict:
    """Fundamental-vocabulary and speculation-vocabulary counts, and whether the title asks."""
    title = (title or "").strip()
    text = f"{title}\n{selftext or ''}".lower()
    return {"fund_n": len(FUND.findall(text)), "spec_n": len(SPEC.findall(text)),
            "is_question": "?" in title or bool(QUESTION.match(title.lower()))}


def build_post_class(parquet_root: Path, out: Path, log=print) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    n = 0
    with pq.ParquetWriter(tmp, PCLASS_SCHEMA, compression="zstd") as w:
        for f in sorted((Path(parquet_root) / "posts").glob("*.parquet")):
            for b in pq.ParquetFile(f).iter_batches(batch_size=50_000, columns=["id", "title", "selftext"]):
                rows = [{"post_id": r["id"], **post_class(r["title"], r["selftext"])} for r in b.to_pylist()]
                w.write_table(pa.Table.from_pylist(rows, schema=PCLASS_SCHEMA))
                n += len(rows)
    tmp.replace(out)
    log(f"[discover] post classes for {n:,} posts -> {out}")
    return n


# ------------------------------------------------------------------ the classes (pre-registered)

S = "n_subst >= 1"
FUNDY = "fund >= 5 and fund >= 2 * spec"
SPECY = "spec >= 3 and spec > fund"
CLASSES = [  # name, family, SQL predicate over the panel, meaning
    ("mentioned", "attention", "n_posts >= 1", "any post naming it"),
    ("substantive", "attention", S, ">= 1 substantive subject post"),
    ("subst_top_decile", "attention", "subst_top", "top decile of substantive count that month (the finalist)"),
    ("novel", "attention", f"{S} and prior_subst = 0", "substantive now, none in the year before the window"),
    ("long_discussed", "attention", f"{S} and prior_subst >= 4", "substantive now and >= 4 in the year before"),
    ("broad", "attention", "n_subst_authors >= 3", ">= 3 distinct authors of substantive posts"),
    ("lone_promoter", "attention", "n_subst >= 2 and n_subst_authors = 1", ">= 2 substantive posts, one author"),
    ("crowded", "attention", "crowded", "top 1% of mentioned companies by posts that month"),
    ("multi_sub", "attention", "n_subst_subs >= 2", "substantive posts in >= 2 subreddits"),
    ("deep_dd", "content", "n_long >= 1", "a substantive self post of >= 3,000 characters"),
    ("primary_source", "content", "n_primary >= 1", "a substantive post linking a primary source"),
    ("fundamentals_talk", "content", f"{S} and {FUNDY}", "fundamental vocabulary dominates"),
    ("speculation_talk", "content", f"{S} and {SPECY}", "speculation / price-action vocabulary dominates"),
    ("questions_only", "content", f"{S} and n_question = n_subst", "every substantive post is a question"),
    ("bear_words", "content", f"{S} and bear > bull", "more bearish than bullish words"),
    ("bull_words", "content", f"{S} and bull > bear", "more bullish than bearish words"),
    ("discussed_24h", "content", f"{S} and c24 >= 20", ">= 20 comments within 24h on its substantive posts"),
    ("ignored_24h", "content", f"{S} and c24 <= 2", "<= 2 comments within 24h"),
    ("subst_small", "state", f"{S} and size_q <= 2", "substantive, two least liquid quintiles"),
    ("subst_large", "state", f"{S} and size_q = 5", "substantive, most liquid quintile"),
    ("subst_beaten_down", "state", f"{S} and mom_q = 1", "substantive, worst 12-1 momentum quintile"),
    ("subst_run_up", "state", f"{S} and mom_q = 5", "substantive, best momentum quintile"),
    ("subst_penny", "state", f"{S} and price < 5", "substantive, price under $5"),
    ("novel_small", "combo", f"{S} and prior_subst = 0 and size_q <= 2", "novel and small"),
    ("novel_fundamentals", "combo", f"{S} and prior_subst = 0 and {FUNDY}", "novel and fundamentals talk"),
    ("contrarian_value", "combo", f"{S} and mom_q = 1 and {FUNDY}", "beaten down and fundamentals talk"),
    ("broad_not_run_up", "combo", "n_subst_authors >= 3 and mom_q in (1, 2, 3, 4)", "broad, not top momentum"),
    ("speculation_small", "combo", f"{S} and {SPECY} and size_q <= 2", "speculation talk on a small name"),
]


def registration_hash() -> str:
    blob = json.dumps([CLASSES, PERIODS, BIG, WINDOW_DAYS, PRIOR_DAYS, Q_BH], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ the panel

def build_panel(con, parquet: Path, mentions: Path, pfeat: Path, pclass: Path, features: Path,
                first: str, last: str, log=print, sic_path: Path | None = None) -> int:
    """One row per eligible company-month in [first, last] with the point-in-time features."""
    bad = ", ".join(f"'{a}'" for a in BAD_AUTHORS)
    rel = ", ".join(f"'{m}'" for m in RELIABLE)
    con.execute(f"create or replace view posts as select * from read_parquet('{parquet}/posts/*.parquet', union_by_name=true)")
    con.execute(f"create or replace view comments as select * from read_parquet('{parquet}/comments/*.parquet', union_by_name=true)")
    con.execute(f"create or replace view universe as select * from read_parquet('{features / 'universe.parquet'}')")
    sic = Path(sic_path) if sic_path else None
    con.execute(f"create or replace view sic as select * from read_parquet('{sic}')" if sic and sic.exists() else
                "create or replace view sic as select null::varchar cik, null::varchar sic, null::varchar division where false")
    con.execute(f"""create or replace temp table months as
        select distinct month, epoch(cast(month as date))::bigint as cutoff from universe
        where eligible and month between '{first}' and '{last}'""")
    # one row per (company, post): visible-text rules of pit.py (not edited after maturation)
    con.execute(f"""create or replace temp table pm as
        with m as (select source_id as post_id, cik, method, n, in_title,
                          count(*) over (partition by source_id) as n_companies
                   from read_parquet('{mentions}') where source = 'post')
        select m.cik, m.post_id, any_value(p.created_utc) created_utc, any_value(p.author) author,
               any_value(p.subreddit) subreddit, any_value(p.is_self) is_self,
               coalesce(any_value(f.f_chars), 0) f_chars, coalesce(any_value(f.f_primary_links), 0) f_primary,
               coalesce(any_value(f.f_bull), 0) f_bull, coalesce(any_value(f.f_bear), 0) f_bear,
               coalesce(any_value(k.fund_n), 0) fund_n, coalesce(any_value(k.spec_n), 0) spec_n,
               coalesce(any_value(k.is_question), false) is_question,
               bool_or((m.method = 'cashtag' or m.in_title or m.n >= 2) and m.n_companies <= 2) and
                 ((any_value(p.is_self) and coalesce(any_value(f.f_chars), 0) >= 400) or
                  (not any_value(p.is_self) and (coalesce(any_value(f.f_chars), 0) >= 200
                                                 or coalesce(any_value(f.f_primary_links), 0) > 0))) subst
        from m join posts p on p.id = m.post_id
        left join read_parquet('{pfeat}') f on f.post_id = m.post_id
        left join read_parquet('{pclass}') k on k.post_id = m.post_id
        where m.method in ({rel}) and m.n_companies <= 10
          and (p.edited_utc = 0 or (p.edited_utc > 0 and p.edited_utc <= p.created_utc + {MATURE}))
        group by m.cik, m.post_id""")
    con.execute(f"""create or replace temp table c24 as
        select c.post_id, count(*) n_c24 from comments c
        join (select distinct post_id, created_utc from pm where subst) s on s.post_id = c.post_id
        where c.created_utc <= s.created_utc + {MATURE} group by c.post_id""")
    con.execute(f"""create or replace temp table agg as
        select mo.month, x.cik,
               count(*) n_posts,
               count(*) filter (where x.subst) n_subst,
               count(distinct x.author) filter (where x.subst and coalesce(x.author, '') not in ({bad})) n_subst_authors,
               count(distinct x.subreddit) filter (where x.subst) n_subst_subs,
               count(*) filter (where x.subst and x.is_self and x.f_chars >= 3000) n_long,
               count(*) filter (where x.subst and x.f_primary > 0) n_primary,
               coalesce(sum(x.fund_n) filter (where x.subst), 0) fund,
               coalesce(sum(x.spec_n) filter (where x.subst), 0) spec,
               count(*) filter (where x.subst and x.is_question) n_question,
               coalesce(sum(x.f_bull) filter (where x.subst), 0) bull,
               coalesce(sum(x.f_bear) filter (where x.subst), 0) bear,
               coalesce(sum(coalesce(c.n_c24, 0)) filter (where x.subst), 0) c24
        from months mo join pm x
          on x.created_utc >= mo.cutoff - {WINDOW_DAYS * DAY} and x.created_utc + {MATURE} <= mo.cutoff
        left join c24 c on c.post_id = x.post_id
        group by mo.month, x.cik""")
    con.execute(f"""create or replace temp table prior as
        select mo.month, x.cik, count(*) prior_subst
        from months mo join pm x
          on x.subst and x.created_utc >= mo.cutoff - {(WINDOW_DAYS + PRIOR_DAYS) * DAY}
             and x.created_utc < mo.cutoff - {WINDOW_DAYS * DAY}
        group by mo.month, x.cik""")
    con.execute(f"""create or replace table panel as
        with u as (
          select month, cik, ticker, size_q, price, adv60,
                 rank() over (partition by month order by adv60 desc nulls last) adv_rank,
                 case when price < 5 then 'p<5' when price < 20 then 'p5-20' else 'p20+' end pbin,
                 case when mom is null or isnan(mom) then 0
                      else ntile(5) over (partition by month, (mom is null or isnan(mom)) order by mom) end mom_q
          from universe where eligible and month between '{first}' and '{last}'),
        j as (
          select u.*, coalesce(a.n_posts, 0) n_posts, coalesce(a.n_subst, 0) n_subst,
                 coalesce(a.n_subst_authors, 0) n_subst_authors, coalesce(a.n_subst_subs, 0) n_subst_subs,
                 coalesce(a.n_long, 0) n_long, coalesce(a.n_primary, 0) n_primary,
                 coalesce(a.fund, 0) fund, coalesce(a.spec, 0) spec, coalesce(a.n_question, 0) n_question,
                 coalesce(a.bull, 0) bull, coalesce(a.bear, 0) bear, coalesce(a.c24, 0) c24,
                 coalesce(p.prior_subst, 0) prior_subst
          from u left join agg a using (month, cik) left join prior p using (month, cik))
        , k as (select j.*, coalesce(sic.division, 'none') division,
                       coalesce(regexp_matches(sic.sic, '^(357|36|48|737)'), false) tech
                from j left join sic using (cik))
        select j.*,
               n_subst >= 1 and percent_rank() over (partition by month, n_subst >= 1 order by n_subst) >= 0.9 subst_top,
               n_posts >= 1 and percent_rank() over (partition by month, n_posts >= 1 order by n_posts) >= 0.99 crowded
        from k j""")
    n, = con.execute("select count(*) from panel").fetchone()
    log(f"[discover] panel: {n:,} eligible company-months {first} .. {last}")
    return n


def build_outcomes(con, features: Path) -> None:
    """Each panel row's outcome at 1, 3 and 5 years next to its matched cell's rates."""
    big = " ".join(f"when {y} then {t}" for y, t in BIG.items())
    con.execute(f"""create or replace temp table ev as
        select p.*, l.years, (l.excess > 0)::double beat, (l.max_mult >= case l.years {big} end)::double big,
               coalesce(l.perm_loss, false)::double loss, l.excess
        from panel p join read_parquet('{features / 'labels.parquet'}') l using (month, cik)
        where l.years in ({", ".join(map(str, BIG))}) and l.status in ('complete', 'delisted')
          and l.excess is not null and not isnan(l.excess)""")
    con.execute("""create or replace table evc as
        with cell as (select month, size_q, mom_q, years, avg(beat) c_beat, avg(big) c_big, avg(loss) c_loss
                      from ev group by all)
        select ev.*, beat - c_beat d_beat, big - c_big d_big, loss - c_loss d_loss
        from ev join cell using (month, size_q, mom_q, years)""")


# ------------------------------------------------------------------ statistics

def cluster_gap(d: np.ndarray, groups: np.ndarray, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Mean of ``d`` with a cluster (Poisson) bootstrap over ``groups``: estimate, 95% CI, se, z."""
    if len(d) == 0:
        return {"n": 0}
    _, inv = np.unique(groups, return_inverse=True)
    s = np.bincount(inv, weights=d)
    c = np.bincount(inv).astype(float)
    w = np.random.default_rng(seed).poisson(1.0, size=(n_boot, len(s))).astype(np.float32)
    draws = (w @ s) / np.maximum(w @ c, 1.0)
    est = float(d.mean())
    se = float(draws.std(ddof=1)) if n_boot > 1 else float("nan")
    return {"n": int(len(d)), "companies": int(len(s)), "gap": est,
            "lo": float(np.percentile(draws, 2.5)), "hi": float(np.percentile(draws, 97.5)),
            "se": se, "z": est / se if se and se > 0 else 0.0}


def norm_sf(z: float) -> float:
    return 0.5 * (1 - erf(z / sqrt(2)))


def bh(pvals: dict[str, float], q: float = Q_BH) -> set[str]:
    """Names passing Benjamini-Hochberg at level q."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m, k = len(items), 0
    for i, (_, p) in enumerate(items, 1):
        if p <= q * i / m:
            k = i
    return {name for name, _ in items[:k]}


def verdicts(res: dict, metric: str) -> dict[str, str]:
    """SURVIVES / CONSISTENT / FAILS per class for one metric, by the pre-registered rule."""
    pvals, ok_sign = {}, {}
    for name, r in res.items():
        d = r["D"][PERIODS["D"][2]][metric]
        c1 = r["C1"][PERIODS["C1"][2]][metric]
        c2 = r["C2"][PERIODS["C2"][2]][metric]
        if not (d.get("n") and c1.get("n") and c2.get("n")):
            continue
        sign = np.sign(d["gap"])
        ok_sign[name] = sign != 0 and np.sign(c1["gap"]) == sign and np.sign(c2["gap"]) == sign
        z = (c1["z"] + c2["z"]) / sqrt(2) * sign
        pvals[name] = norm_sf(z)
        r.setdefault("confirm_p", {})[metric] = pvals[name]
    passed = bh(pvals)
    out = {}
    for name, r in res.items():
        if name not in ok_sign:
            out[name] = "NO DATA"
            continue
        d = r["D"][PERIODS["D"][2]][metric]
        d_sig = d["lo"] > 0 or d["hi"] < 0
        out[name] = ("SURVIVES" if d_sig and ok_sign[name] and name in passed
                     else "CONSISTENT" if ok_sign[name] else "FAILS")
    return out


def evaluate(con, n_boot: int = N_BOOT, log=print) -> dict:
    res = {}
    for name, family, pred, meaning in CLASSES:
        r = {"family": family, "pred": pred, "meaning": meaning}
        for per, (first, last, h0, other) in PERIODS.items():
            r[per] = {}
            for h in (h0, *other):
                rows = con.execute(f"""select cik, d_beat, d_big, d_loss, beat, big, loss, substr(month, 1, 4) yr
                                       from evc where years = {h} and month between '{first}' and '{last}' and ({pred})""").fetchall()
                arr = np.array([r_[1:7] for r_ in rows], float) if rows else np.zeros((0, 6))
                cik = np.array([r_[0] for r_ in rows])
                r[per][h] = {m: {**cluster_gap(arr[:, i], cik, n_boot), "rate": float(arr[:, i + 3].mean()) if len(arr) else None}
                             for i, m in enumerate(("beat", "big", "loss"))}
                if per == "D" and h == h0 and rows:
                    yrs = np.array([r_[7] for r_ in rows])
                    gaps = {y: float(arr[yrs == y, 0].mean()) for y in sorted(set(yrs))}
                    r["D_years"] = gaps
        res[name] = r
        log(f"  {name}: done")
    return res


# ------------------------------------------------------------------ round 2: robustness

# Fixed 2026-11-12 after round 1 and before computing any of it: the classes that survived round
# 1 (beat for the breadth family, loss for the small / penny family), each re-tested six ways and
# by half-period. A finding is ROBUST only if it keeps its round-1 sign in every variant x period
# cell and in at least 5 of the 6 half-periods; it is FRAGILE otherwise. Round 1's verdict stands
# either way; this says whether to believe it will carry forward.
ROBUST = [("subst_top_decile", "beat"), ("broad", "beat"), ("crowded", "beat"), ("multi_sub", "beat"),
          ("discussed_24h", "beat"), ("broad_not_run_up", "beat"),
          ("subst_small", "loss"), ("novel_small", "loss"), ("subst_penny", "loss")]
MEGACAP_RANK = 100
CELL = ("month", "size_q", "mom_q")
VARIANTS = {  # name -> (extra cell keys, rows excluded from both sides)
    "as round 1": ((), "false"),
    "sector-matched": (("division",), "false"),
    "no top-100 liquidity": ((), f"adv_rank <= {MEGACAP_RANK}"),
    "no tech": ((), "tech"),
    "price-matched": (("pbin",), "false"),
}
HALVES = {"D": [("2012-01-01", "2015-12-01"), ("2016-01-01", "2018-12-01")],
          "C1": [("2019-01-01", "2020-12-01"), ("2021-01-01", "2022-12-01")],
          "C2": [("2023-01-01", "2024-12-01"), ("2025-01-01", "2025-09-01")]}


def diffs(con, pred: str, metric: str, h: int, first: str, last: str, keys=CELL, exclude: str = "false"):
    """(cik, member minus own-cell rate) for the class members of one period and horizon."""
    k = ", ".join(keys)
    rows = con.execute(f"""
        with base as (select * from ev where years = {h} and month between '{first}' and '{last}'
                      and not ({exclude})),
             cell as (select {k}, avg({metric}) c from base group by {k})
        select cik, {metric} - c from base join cell using ({k}) where ({pred})""").fetchall()
    return np.array([r[0] for r in rows]), np.array([r[1] for r in rows], float)


def company_weighted(cik, d, n_boot):
    if not len(d):
        return {"n": 0}
    u, inv = np.unique(cik, return_inverse=True)
    means = np.bincount(inv, weights=d) / np.bincount(inv)
    return {**cluster_gap(means, np.arange(len(u)), n_boot), "n": int(len(d))}


def drop_top(cik, d, sign, k=5):
    if not len(d):
        return cik, d
    u, inv = np.unique(cik, return_inverse=True)
    contrib = np.bincount(inv, weights=d) * (sign or 1)
    top = set(u[np.argsort(-contrib)[:k]])
    keep = np.array([c not in top for c in cik])
    return cik[keep], d[keep]


def robustness(con, n_boot: int = N_BOOT, log=print) -> dict:
    preds = {name: pred for name, _, pred, _ in CLASSES}
    out = {}
    for name, metric in ROBUST:
        pred, r = preds[name], {}
        for per, (first, last, h, _) in PERIODS.items():
            for var, (extra, excl) in VARIANTS.items():
                cik, d = diffs(con, pred, metric, h, first, last, CELL + extra, excl)
                r.setdefault(var, {})[per] = cluster_gap(d, cik, n_boot)
            cik, d = diffs(con, pred, metric, h, first, last)
            r.setdefault("equal company weight", {})[per] = company_weighted(cik, d, n_boot)
            c2, dd = drop_top(cik, d, np.sign(r["as round 1"]["D"].get("gap", 0)))
            r.setdefault("top-5 companies left out", {})[per] = cluster_gap(dd, c2, n_boot)
            r["companies"] = {**r.get("companies", {}), per: int(len(set(cik)))}
            for i, (f, l) in enumerate(HALVES[per]):
                cik, d = diffs(con, pred, metric, h, f, l)
                r.setdefault("halves", {})[f"{per}{'ab'[i]}"] = cluster_gap(d, cik, n_boot)
        sign = np.sign(r["as round 1"]["D"].get("gap", 0))
        cells = [g for var, v in r.items() if var not in ("halves", "companies") for g in v.values()]
        ok_cells = all(g.get("n") and np.sign(g["gap"]) == sign for g in cells)
        ok_halves = sum(1 for g in r["halves"].values() if g.get("n") and np.sign(g["gap"]) == sign)
        r["verdict"] = "ROBUST" if ok_cells and ok_halves >= 5 else "FRAGILE"
        r["metric"], r["halves_same_sign"] = metric, ok_halves
        out[name] = r
        log(f"\n== {name} ({metric} gap vs matched peers, points; companies D/C1/C2 "
            f"{r['companies']['D']}/{r['companies']['C1']}/{r['companies']['C2']})")
        for var, v in r.items():
            if var in ("halves", "companies", "verdict", "metric", "halves_same_sign"):
                continue
            log(f"  {var:26s} " + "  ".join(f"{per} {fmt(v[per])}" for per in PERIODS))
        log("  halves                     " + "  ".join(f"{k} {g['gap'] * 100:+5.1f}" if g.get("n") else f"{k}  n/a"
                                                    for k, g in r["halves"].items()))
        log(f"  -> {r['verdict']} ({ok_halves}/6 halves with the sign)")
    return out


# ------------------------------------------------------------------ report

def fmt(g: dict, scale=100) -> str:
    if not g.get("n"):
        return "        n/a        "
    return f"{g['gap'] * scale:+5.1f} [{g['lo'] * scale:+5.1f},{g['hi'] * scale:+5.1f}]"


def report(res: dict, verdict: dict[str, dict[str, str]], log=print) -> None:
    hD, h1, h2 = (PERIODS[p][2] for p in ("D", "C1", "C2"))
    log(f"\nGap vs matched peers, percentage points [95% CI, clustered by company]; "
        f"D = 2012-2018 at {hD}y, C1 = 2019-2022 at {h1}y, C2 = 2023-2025/09 at {h2}y")
    for metric in ("beat", "big", "loss"):
        log(f"\n-- {metric}: " + {"beat": "beat the S&P", "big": "best multiple >= 2x (1y) / 3x (3y) / 5x (5y)",
                                  "loss": "permanent loss"}[metric])
        log(f"  {'class':20s} {'n D':>6s} {'D':^21s} yrs  {'n C1':>6s} {'C1':^21s} {'n C2':>6s} {'C2':^21s} "
            f"{'D 1y':^21s}  verdict")
        for name, r in res.items():
            d, c1, c2 = r["D"][hD][metric], r["C1"][h1][metric], r["C2"][h2][metric]
            d1 = r["D"].get(1, {}).get(metric, {})
            yrs = r.get("D_years", {})
            same = sum(1 for g in yrs.values() if np.sign(g) == np.sign(d.get("gap", 0)))
            log(f"  {name:20s} {d.get('n', 0):6d} {fmt(d)} {same}/{len(yrs)}  {c1.get('n', 0):6d} {fmt(c1)} "
                f"{c2.get('n', 0):6d} {fmt(c2)} {fmt(d1)}  {verdict[metric][name]}")
    sub = res.get("substantive")
    if sub:
        log("\nincremental over 'substantive' (beat gap, class minus substantive, same period):")
        for name, r in res.items():
            if r["family"] in ("content", "state", "combo"):
                parts = []
                for per in PERIODS:
                    h = PERIODS[per][2]
                    a, b = r[per][h]["beat"], sub[per][h]["beat"]
                    parts.append(f"{per} {((a['gap'] - b['gap']) * 100):+5.1f}" if a.get("n") and b.get("n") else f"{per}  n/a")
                log(f"  {name:20s} " + "  ".join(parts))


def examples(con, name: str, pred: str, log=print, k: int = 12) -> None:
    rows = con.execute(f"""select month, ticker, n_subst, n_subst_authors, prior_subst, size_q, mom_q,
                                  round(excess * 100) ex, big, loss
                           from evc where years = 1 and month >= '2023-01-01' and ({pred})
                           order by hash(month || cik) limit {k}""").fetchall()
    log(f"  {name}: " + "; ".join(f"{m[:7]} {t} subst {s} auth {a} prior {p} q{sq}/m{mq} 1y {ex:+.0f}%"
                                  for m, t, s, a, p, sq, mq, ex, *_ in rows))


def show(res: dict, names: list[str], log=print) -> None:
    """Class rate and implied peer rate (class rate minus gap) per period, horizon and metric."""
    for name in names or list(res["classes"]):
        r = res["classes"][name]
        log(f"{name}: {r['meaning']}")
        for per, by_h in ((k, r[k]) for k in PERIODS):
            for h, ms in by_h.items():
                log(f"  {per} {h}y  " + "  ".join(
                    f"{m} {g['rate'] * 100:4.1f}% vs peers {(g['rate'] - g['gap']) * 100:4.1f}% (n {g['n']})"
                    for m, g in ms.items() if g.get("n")))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--post-features", default="data/features/post_features.parquet")
    p.add_argument("--post-class", default="data/features/post_class.parquet")
    p.add_argument("--features", default="data/features")
    p.add_argument("--first", default="2012-01-01")
    p.add_argument("--last", default="2025-09-01")
    p.add_argument("--out", default="data/experiments/discover_v1.json")
    p.add_argument("--boot", type=int, default=N_BOOT)
    p.add_argument("--sic", default="data/ref/sic.parquet")
    p.add_argument("--robust", action="store_true", help="round 2: the robustness checks of round 1's survivors")
    p.add_argument("--show", nargs="*", metavar="CLASS",
                   help="print the class and implied peer rates from a finished round-1 result (--out), then stop")
    a = p.parse_args(argv)
    if a.show is not None:
        show(json.loads(Path(a.out).read_text()), a.show)
        return 0
    import duckdb
    feats = Path(a.features)
    pclass = Path(a.post_class)
    if not pclass.exists():
        build_post_class(Path(a.parquet), pclass)
    print(f"[discover] registration {registration_hash()}: {len(CLASSES)} classes, periods "
          + ", ".join(f"{k} {v[0][:7]}..{v[1][:7]} at {v[2]}y" for k, v in PERIODS.items()), flush=True)
    con = duckdb.connect()
    build_panel(con, Path(a.parquet), Path(a.mentions), Path(a.post_features), pclass, feats, a.first, a.last,
                sic_path=Path(a.sic))
    build_outcomes(con, feats)
    if a.robust:
        rob = robustness(con, a.boot)
        out = Path(a.out).with_name(Path(a.out).stem + "_robust.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rob, default=str, indent=1))
        print("\n[discover] robustness: " + ", ".join(f"{k} {v['verdict']}" for k, v in rob.items()), flush=True)
        print(f"[discover] wrote {out}", flush=True)
        return 0
    for per, (first, last, h, _) in PERIODS.items():
        n, base = con.execute(f"select count(*), avg(beat) from evc where years = {h} and month between "
                              f"'{first}' and '{last}'").fetchone()
        print(f"[discover] {per}: {n:,} company-months with a final {h}y outcome, {base * 100 if n else 0:.1f}% beat the S&P",
              flush=True)
    res = evaluate(con, a.boot)
    verdict = {m: verdicts(res, m) for m in ("beat", "big", "loss")}
    report(res, verdict)
    print("\nexamples from C2 (2023-2025, 1y excess vs the S&P), for a sanity read:")
    for name, _, pred, _ in CLASSES:
        if verdict["beat"][name] == "SURVIVES":
            examples(con, name, pred)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"registration": registration_hash(), "periods": PERIODS, "classes": res,
                               "verdicts": verdict}, default=str, indent=1))
    print(f"\n[discover] wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
