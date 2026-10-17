#!/usr/bin/env python3
"""Causal backtest engine: monthly cohorts, candidate-discovery metrics.

Per cohort month ``c`` a ``Scorer`` sees only the point-in-time corpus up to ``c`` (pit.py)
and the eligible universe, and returns scores. The engine then takes the **top fraction
of scored companies each month** and measures what happened from the open after ``c``:

* P(2x / 5x / 10x) within 1/3/5 years (closes), P(permanent loss: <= -80% at the end),
  P(-50% / -80% touched), CAGR and excess-return quantiles;
* **top-decile lift** = P(event | selected) / P(event | eligible universe) (also vs the
  mentioned-but-not-selected set), with block-bootstrap intervals; **10x recall**;
* a Kaplan-Meier estimate of P(5x/10x by horizon) that uses censored recent cohorts
  properly (fixed-horizon rates exclude them instead of counting them as failures);
* a permutation test (scores shuffled within month x size bucket);
* a monthly overlapping-lots portfolio diagnostic (CAGR, vol, Sharpe/Sortino, max drawdown,
  information ratio vs SPY, concentration, turnover) and its deflated Sharpe.

Everything evaluated is written to the experiment ledger; the sealed holdout can be looked
at once (ledger.py). Ties in scores are broken by a seeded hash, never by input order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger as lg  # noqa: E402
import prices as px  # noqa: E402
import stats  # noqa: E402
from scorer import Cohort, Scorer, config_hash  # noqa: E402

HORIZONS = (1, 3, 5)
LABEL_COLS = ("status", "ret", "cagr", "excess", "max_mult", "min_mult", "max_dd", "d2", "d5",
              "d10", "perm_loss", "entry_day", "end_day")
_NAN_COLS = {"d2", "d5", "d10", "cagr", "excess"}


def label_select(alias: str = "l") -> str:
    """Select list for the label columns; nullable numerics become double with NaN for NULL."""
    return ", ".join(f"coalesce(cast({alias}.{c} as double), 'nan'::double) as {c}"
                     if c in _NAN_COLS else f"{alias}.{c}" for c in LABEL_COLS)
SCORE_SCHEMA = pa.schema([("month", pa.string()), ("cik", pa.string()), ("score", pa.float64()),
                          ("extra", pa.string())])


def month_utc(month: str) -> int:
    return px.to_day(month) * 86400


# ---------------------------------------------------------------- scoring

def universe_by_month(con, months: list[str]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict] = {m: {} for m in months}
    marks = ",".join("?" * len(months))
    cur = con.execute(f"select * from universe where eligible and month in ({marks})", months)
    cols = [d[0] for d in cur.description]
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        out[r["month"]][r["cik"]] = r
    return out


def data_stamp(*paths) -> str:
    """Fingerprint of the input data files (size + mtime), so cached scores die with the data
    they were computed from. Missing paths are ignored."""
    parts = []
    for p in paths:
        if p and Path(p).exists():
            st = Path(p).stat()
            parts.append(f"{Path(p).name}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def build_scores(scorer: Scorer, months: list[str], corpus, con, cache_dir: Path | None = None,
                 log=print, stamp: str = "") -> pa.Table:
    """Score every cohort month (cached per scorer configuration and data fingerprint)."""
    cid = config_hash(scorer, {"data": stamp} if stamp else None)
    cache = (Path(cache_dir) / cid / "scores.parquet") if cache_dir else None
    have = pq.read_table(cache).to_pylist() if cache and cache.exists() else []
    done = {r["month"] for r in have}
    todo = [m for m in months if m not in done]
    uni = universe_by_month(con, todo) if todo else {}
    rows = list(have)
    for m in todo:
        cutoff = month_utc(m)
        cohort = Cohort(m, cutoff, corpus.at(cutoff, scorer.lookback_days), uni[m])
        res = scorer.score(cohort)
        for cik, d in res.items():
            if cik in uni[m] and d.get("score") is not None and math.isfinite(d["score"]):
                rows.append({"month": m, "cik": cik, "score": float(d["score"]),
                             "extra": json.dumps({k: v for k, v in d.items() if k != "score"})})
        done.add(m)
        log(f"  scored {m}: {sum(1 for r in rows if r['month'] == m):,} candidates")
    table = pa.Table.from_pylist(rows, schema=SCORE_SCHEMA)
    if cache and todo:
        cache.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, cache)
    wanted = set(months)
    return table.filter(pa.compute.is_in(table["month"], pa.array(sorted(wanted))))


# ---------------------------------------------------------------- selection

def _tiebreak(seed: int, month: str, cik: str) -> float:
    return int(hashlib.sha1(f"{seed}|{month}|{cik}".encode()).hexdigest()[:10], 16) / 16 ** 10


def select_top(months, ciks, scores, top_frac: float, *, min_candidates: int = 10,
               seed: int = 0) -> np.ndarray:
    """Boolean mask: the top ``top_frac`` of each month's candidates by score. Months with
    fewer than ``min_candidates`` scored companies select nobody."""
    months, scores = np.asarray(months), np.asarray(scores, dtype=float)
    mask = np.zeros(len(scores), dtype=bool)
    for m in np.unique(months):
        idx = np.nonzero(months == m)[0]
        if len(idx) < min_candidates:
            continue
        ties = np.array([_tiebreak(seed, m, ciks[i]) for i in idx])
        order = np.lexsort((ties, -scores[idx]))     # best score first, hash breaks ties
        k = max(1, math.ceil(top_frac * len(idx)))
        mask[idx[order[:k]]] = True
    return mask


def dedupe_selection(months, ciks, mask: np.ndarray, window: int) -> np.ndarray:
    """Keep a company's selection only if it was not selected in the previous ``window``
    cohort months: one sustained story is one discovery, not twelve."""
    if window <= 0:
        return mask
    out = mask.copy()
    last: dict[str, int] = {}
    order = np.argsort(np.array([m for m in months]), kind="stable")
    index = {m: i for i, m in enumerate(sorted(set(months)))}
    for i in order:
        if not mask[i]:
            continue
        t = index[months[i]]
        if ciks[i] in last and t - last[ciks[i]] <= window:
            out[i] = False
        else:
            last[ciks[i]] = t
    return out


# ---------------------------------------------------------------- horizon metrics

def final_mask(a: dict) -> np.ndarray:
    """Holdings whose outcome is final and trustworthy: not censored, not bad data."""
    return (a["status"] != "censored") & (a["status"] != "bad_data")


def event_rates(a: dict) -> dict:
    """Fixed-horizon rates over holdings whose outcome is final (not censored)."""
    final = final_mask(a)
    n = int(final.sum())
    if n == 0:
        return {"n": 0, "bad_data": int((a["status"] == "bad_data").sum())}
    mm, lo = a["max_mult"][final], a["min_mult"][final]
    out = {"n": n, "p2": float((mm >= 2).mean()), "p5": float((mm >= 5).mean()),
           "p10": float((mm >= 10).mean()), "p_loss": float(a["perm_loss"][final].mean()),
           "p_dd50": float((lo <= 0.5).mean()), "p_dd80": float((lo <= 0.2).mean()),
           "cagr": stats.quantiles(a["cagr"][final]), "excess": stats.quantiles(a["excess"][final]),
           "mean_excess": float(np.nanmean(a["excess"][final])) if np.isfinite(a["excess"][final]).any() else float("nan"),
           "delisted_share": float((a["status"][final] == "delisted").mean()),
           "bad_data": int((a["status"] == "bad_data").sum())}
    return out


def km_touch(a: dict, k: int, years: int) -> float:
    """Kaplan-Meier P(close >= k x entry within ``years``), censored cohorts included."""
    horizon = years * px.YEAR_DAYS
    ok = a["status"] != "bad_data"
    d = a[f"d{k}"][ok]
    hit = np.isfinite(d)
    follow = np.where(a["status"][ok] == "complete", horizon,
                      np.minimum(a["end_day"][ok] - a["entry_day"][ok], horizon)).astype(float)
    dur = np.where(hit, d, follow)
    return stats.km_event_prob(dur, hit, horizon)


def _lift(p_top: float, p_ref: float) -> float:
    return p_top / p_ref if p_ref and np.isfinite(p_top) and np.isfinite(p_ref) else float("nan")


EVENT_COLS = {"p2": ("max_mult", 2), "p5": ("max_mult", 5), "p10": ("max_mult", 10),
              "p_loss": ("perm_loss", None)}


def _event(a: dict, name: str) -> np.ndarray:
    col, k = EVENT_COLS[name]
    return a[col].astype(bool) if k is None else (a[col] >= k)


def matched_lift(sel: dict, uni: dict, strata_key: str = "size_q") -> dict:
    """Lift against the universe re-weighted to the selected set's mix of ``strata_key``
    (size/liquidity buckets): P(event | selected) / sum_q w_q P(event | universe, bucket q)
    with w_q the share of selected holdings in bucket q.

    The plain lift compares the picks with *every* eligible stock; this one compares them
    with the same kind of stock. The gap between the two is how much of the plain lift is
    just exposure to volatile small names rather than picking well within them."""
    fs, fu = final_mask(sel), final_mask(uni)
    if not fs.any() or not fu.any():
        return {}
    ks, ku = sel[strata_key][fs], uni[strata_key][fu]
    out = {}
    for name in EVENT_COLS:
        es, eu = _event(sel, name)[fs], _event(uni, name)[fu]
        p_sel = float(es.mean())
        num = den = 0.0
        for q in np.unique(ks):
            w = float((ks == q).mean())
            in_u = ku == q
            if in_u.any():
                num += w * float(eu[in_u].mean())
                den += w
        out[name] = _lift(p_sel, num / den) if den > 0 else float("nan")
    return out


# ---------------------------------------------------------------- the evaluator

class Evaluator:
    def __init__(self, features_dir: Path = Path("data/features"), *, sic: Path | None = None):
        self.dir = Path(features_dir)
        self.con = duckdb.connect()
        self.con.execute(f"create view universe as select * from read_parquet('{self.dir}/universe.parquet')")
        self.con.execute(f"create view labels as select * from read_parquet('{self.dir}/labels.parquet')")
        self.has_sic = bool(sic and Path(sic).exists())
        if self.has_sic:
            self.con.execute(f"create view sic as select * from read_parquet('{sic}')")

    def months(self) -> list[str]:
        return [r[0] for r in self.con.execute("select distinct month from universe order by 1").fetchall()]

    def _fetch(self, sql: str, params=None) -> dict:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        data = cur.fetchnumpy()
        return {c: np.asarray(data[c].filled(np.nan) if np.ma.isMaskedArray(data[c]) and
                              data[c].dtype.kind == "f" else data[c]) for c in cols}

    def joined(self, scores: pa.Table, years: int, months: list[str]) -> dict:
        self.con.register("scores", scores)
        cols = label_select()
        extra = ", sic.division as division" if self.has_sic else ""
        join = "left join sic on sic.cik = s.cik" if self.has_sic else ""
        return self._fetch(f"""
            select s.month, s.cik, s.score, u.size_q, u.ticker, u.code, {cols}{extra}
            from scores s join universe u on u.month = s.month and u.cik = s.cik
            join labels l on l.month = s.month and l.cik = s.cik and l.years = {int(years)}
            {join} where u.eligible order by s.month, s.cik""")

    def universe_labels(self, years: int, months: list[str]) -> dict:
        marks = ",".join("?" * len(months))
        cols = label_select()
        return self._fetch(f"""
            select u.month, u.cik, u.size_q, {cols}
            from universe u join labels l on l.month = u.month and l.cik = u.cik
                 and l.years = {int(years)}
            where u.eligible and u.month in ({marks}) order by u.month, u.cik""", months)

    # ------------------------------------------------------------ one horizon

    def horizon_report(self, scores: pa.Table, years: int, months: list[str], *, top_frac=0.1,
                       min_candidates=10, seed=0, n_boot=500, n_perm=300, size_q=None,
                       dedupe_months: int = 0) -> dict:
        cand = self.joined(scores, years, months)
        uni = self.universe_labels(years, months)
        if size_q:
            keep = np.isin(cand["size_q"], size_q)
            cand = {k: v[keep] for k, v in cand.items()}
            ukeep = np.isin(uni["size_q"], size_q)
            uni = {k: v[ukeep] for k, v in uni.items()}
        if len(cand["month"]) == 0:
            return {"years": years, "n_candidates": 0}
        top = select_top(cand["month"], cand["cik"], cand["score"], top_frac,
                         min_candidates=min_candidates, seed=seed)
        top = dedupe_selection(cand["month"], cand["cik"], top, dedupe_months)
        sel = {k: v[top] for k, v in cand.items()}
        rest = {k: v[~top] for k, v in cand.items()}
        rep = {"years": years, "n_candidates": int(len(top)), "n_selected": int(top.sum()),
               "n_universe": int(len(uni["month"])), "unique_selected": int(len(set(sel["cik"]))),
               "top": event_rates(sel), "mentioned_rest": event_rates(rest),
               "universe": event_rates(uni)}
        for k in (5, 10):
            rep[f"km_p{k}"] = {"top": km_touch(sel, k, years), "universe": km_touch(uni, k, years)}
        t, u, r = rep["top"], rep["universe"], rep["mentioned_rest"]
        if t["n"] and u["n"]:
            rep["lift"] = {k: _lift(t[k], u[k]) for k in ("p2", "p5", "p10", "p_loss")}
            rep["lift_matched"] = matched_lift(sel, uni)
            rep["lift_vs_mentioned"] = {k: _lift(t[k], r.get(k, float("nan")))
                                        for k in ("p2", "p5", "p10", "p_loss")} if r["n"] else {}
            rep["recall10"] = self._recall(sel, uni, 10)
            rep["lift_ci"] = self._bootstrap(sel, uni, n_boot, seed)
            rep["perm_p5"] = self._permute(cand, top_frac, n_perm, seed, min_candidates)
        return rep

    @staticmethod
    def _recall(sel: dict, uni: dict, k: int) -> float:
        final = final_mask(uni)
        winners = {(m, c) for m, c, mm, f in zip(uni["month"], uni["cik"], uni["max_mult"], final)
                   if f and mm >= k}
        got = {(m, c) for m, c, mm, s in zip(sel["month"], sel["cik"], sel["max_mult"], sel["status"])
               if s not in ("censored", "bad_data") and mm >= k}
        return len(got & winners) / len(winners) if winners else float("nan")

    @staticmethod
    def _quarter(m: str) -> str:
        return f"{m[:4]}Q{(int(m[5:7]) - 1) // 3 + 1}"

    def _bootstrap(self, sel: dict, uni: dict, n: int, seed: int) -> dict:
        def counts(a):
            final = final_mask(a)
            q = np.array([self._quarter(m) for m in a["month"]])
            res = {}
            for qq in np.unique(q):
                i = (q == qq) & final
                res[qq] = np.array([i.sum(), (a["max_mult"][i] >= 2).sum(), (a["max_mult"][i] >= 5).sum(),
                                    (a["max_mult"][i] >= 10).sum(), a["perm_loss"][i].sum()], float)
            return res
        cs, cu = counts(sel), counts(uni)
        quarters = sorted(set(cs) & set(cu))
        if len(quarters) < 4:
            return {}
        zero = np.zeros(5)
        blocks = {q: np.concatenate([cs.get(q, zero), cu[q]]) for q in quarters}
        out = {}
        for j, name in enumerate(("p2", "p5", "p10", "p_loss"), start=1):
            def stat(bs, j=j):
                tot = np.sum(bs, axis=0)
                return _lift(tot[j] / tot[0] if tot[0] else float("nan"),
                             tot[5 + j] / tot[5] if tot[5] else float("nan"))
            lo, hi = stats.percentile_ci(stats.block_bootstrap(blocks, stat, n, seed))
            out[name] = (lo, hi)
        return out

    def _permute(self, cand: dict, top_frac: float, n: int, seed: int, min_candidates: int) -> float:
        """p-value for P(5x | top) against shuffling outcomes within month x size strata."""
        final = final_mask(cand)
        sector = cand.get("division")
        keys = np.array([f"{m}|{q}|{sector[i] if sector is not None else ''}"
                         for i, (m, q) in enumerate(zip(cand["month"], cand["size_q"]))])
        strata = []
        for kk in np.unique(keys):
            i = np.nonzero((keys == kk) & final)[0]
            if len(i) >= min_candidates:
                strata.append((cand["score"][i], (cand["max_mult"][i] >= 5).astype(float)))

        def stat(ss):
            hit = tot = 0
            for s, o in ss:
                k = max(1, math.ceil(top_frac * len(s)))
                hit += o[np.argsort(-s, kind="stable")[:k]].sum()
                tot += k
            return hit / tot if tot else float("nan")
        if not strata:
            return float("nan")
        return stats.permutation_p(strata, stat, stat(strata), n, seed)


# ---------------------------------------------------------------- portfolio diagnostic

def month_end_values(s: px.Series) -> tuple[np.ndarray, np.ndarray]:
    """(month index array, last adjusted close in each month); month index = year*12+month-1."""
    ms = np.array(px.DAY0 + s.days.astype("timedelta64[D]"), dtype="datetime64[M]").astype(int)
    first = ms[0]
    last_of = np.append(np.nonzero(np.diff(ms))[0], len(ms) - 1)
    return ms[last_of] + 1970 * 12, s.close[last_of]


def lot_returns(s: px.Series, cohort_month: str, entry_i: int, hold_months: int,
                slippage_bps: float) -> list[float]:
    """Monthly returns of one position for ``hold_months`` months from the cohort month."""
    mi, mv = month_end_values(s)
    y, m = int(cohort_month[:4]), int(cohort_month[5:7])
    start = y * 12 + m - 1
    prev = s.open[entry_i] * (1 + slippage_bps / 1e4)
    out = []
    for k in range(hold_months):
        j = np.searchsorted(mi, start + k, side="right") - 1
        if j < 0:
            out.append(0.0)
            continue
        cur = mv[j]
        out.append(float(cur / prev - 1))
        prev = cur
    return out


def simulate_portfolio(picks: list[tuple[str, str, str]], store: px.PriceStore, hold_months: int,
                       slippage_bps: float = 25.0) -> dict:
    """Overlapping lots: every cohort month buys the picked names equal-weight and holds
    ``hold_months``; the portfolio is the equal-weight average of active lots. Delisted
    names stay flat at their last close. ``picks`` = (cohort_month, cik, price code)."""
    bench = store.get("SPY")
    lots: dict[str, list[list[float]]] = {}
    held = set()
    finals = []
    for month, cik, code in picks:
        s = store.get(code)
        if s is None:
            continue
        e = s.entry_index(px.to_day(month) + 1)
        if e is None:
            continue
        end = int(s.days[e]) + int(hold_months * 30.5)
        if len(s.bad_days) and ((s.bad_days >= s.days[e]) & (s.bad_days <= end)).any():
            continue   # untrustworthy prices inside the hold: excluded, as in the label statistics
        r = lot_returns(s, month, e, hold_months, slippage_bps)
        lots.setdefault(month, []).append(r)
        held.add((month, cik))
        finals.append(float(np.prod(1 + np.array(r)) - 1))
    if not lots:
        return {}
    months = sorted(lots)
    y0, m0 = int(months[0][:4]), int(months[0][5:7])
    start = y0 * 12 + m0 - 1
    horizon = (int(months[-1][:4]) * 12 + int(months[-1][5:7]) - 1) - start + hold_months
    series = []
    for t in range(horizon):
        active = []
        for mo, rs in lots.items():
            k = t - ((int(mo[:4]) * 12 + int(mo[5:7]) - 1) - start)
            if 0 <= k < hold_months:
                active.append(np.mean([r[k] for r in rs]))
        series.append(float(np.mean(active)) if active else 0.0)
    ys, ms_ = start // 12, start % 12 + 1
    bench_r = []
    if bench is not None:
        bi, bv = month_end_values(bench)
        for t in range(horizon):
            idx = start + t
            a = np.searchsorted(bi, idx - 1, side="right") - 1
            b = np.searchsorted(bi, idx, side="right") - 1
            bench_r.append(float(bv[b] / bv[a] - 1) if a >= 0 and b >= 0 else 0.0)
    r = np.array(series)
    br = np.array(bench_r) if bench_r else np.zeros_like(r)
    nav = np.cumprod(1 + r)
    dd = float(np.min(nav / np.maximum.accumulate(nav) - 1))
    ex = r - br
    sr, skew, kurt, n = stats.sharpe_moments(r)
    esr, eskew, ekurt, en = stats.sharpe_moments(ex)   # the deflated Sharpe tests this one
    down = r[r < 0]
    best = np.sort(np.array(finals))[::-1]
    gains = np.clip(best, 0, None)
    return {
        "months": len(r), "cagr": float(nav[-1] ** (12 / len(r)) - 1),
        "vol": float(r.std(ddof=1) * math.sqrt(12)), "sharpe_monthly": sr, "skew": skew, "kurt": kurt,
        "sharpe": sr * math.sqrt(12) if np.isfinite(sr) else float("nan"),
        "sortino": float(r.mean() / down.std(ddof=1) * math.sqrt(12)) if len(down) > 2 and down.std(ddof=1) > 0 else float("nan"),
        "max_dd": dd, "bench_cagr": float(np.prod(1 + br) ** (12 / len(br)) - 1),
        "info_ratio": float(ex.mean() / ex.std(ddof=1) * math.sqrt(12)) if ex.std(ddof=1) > 0 else float("nan"),
        "name_hit_rate": float((np.array(finals) > 0).mean()),
        "top5_share_of_gains": float(gains[:5].sum() / gains.sum()) if gains.sum() > 0 else float("nan"),
        "positions": len(finals), "unique_names": len({c for _, c, _ in picks}),
        "n_obs": n, "excess_sharpe_monthly": esr, "excess_skew": eskew, "excess_kurt": ekurt,
        "excess_n": en,
    }


# ---------------------------------------------------------------- orchestration

def run(scorer: Scorer, evaluator: Evaluator, corpus, split: str, *, ledger: lg.Ledger,
        top_frac: float = 0.1, horizons=HORIZONS, min_candidates: int = 10, seed: int = 0,
        n_boot: int = 500, n_perm: int = 300, size_q=None, hold_months: int = 36,
        dedupe_months: int = 0,
        store: px.PriceStore | None = None, cache_dir: Path | None = Path("data/experiments"),
        accept_contaminated: bool = False, log=print) -> dict:
    engine = {"top_frac": top_frac, "min_candidates": min_candidates, "seed": seed,
              "horizons": list(horizons), "size_q": list(size_q) if size_q else None,
              "hold_months": hold_months, "dedupe_months": dedupe_months}
    cid = config_hash(scorer, engine)
    ledger.check(split, cid, accept_contaminated=accept_contaminated)   # before any work
    months = lg.split_months(split, evaluator.months())
    stamp = data_stamp(getattr(corpus, "mentions_path", None), getattr(corpus, "post_features", None),
                       evaluator.dir / "universe.parquet")
    scores = build_scores(scorer, months, corpus, evaluator.con, cache_dir, log, stamp)
    report = {"config_id": cid, "split": split, "scorer": scorer.name, "months": len(months),
              "horizons": {}}
    for h in horizons:
        report["horizons"][h] = evaluator.horizon_report(
            scores, h, months, top_frac=top_frac, min_candidates=min_candidates, seed=seed,
            n_boot=n_boot, n_perm=n_perm, size_q=size_q, dedupe_months=dedupe_months)
    if store is not None:
        cand = evaluator.joined(scores, horizons[0], months)
        top = select_top(cand["month"], cand["cik"], cand["score"], top_frac,
                         min_candidates=min_candidates, seed=seed)
        top = dedupe_selection(cand["month"], cand["cik"], top, dedupe_months)
        picks = [(m, c, k) for m, c, k, t in zip(cand["month"], cand["cik"], cand["code"], top) if t]
        report["portfolio"] = simulate_portfolio(picks, store, hold_months)
        pr = report["portfolio"]
        if pr:
            n_tr = max(ledger.n_trials() + 1, 1)
            # Sharpe of returns *in excess of SPY*: a long-only equity portfolio has a positive raw
            # Sharpe in any bull market, so testing that would flatter everything, even random picks
            srs = ledger.trial_sharpes() + [pr["excess_sharpe_monthly"]]
            var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.01
            pr["deflated_sharpe"] = stats.deflated_sharpe(
                pr["excess_sharpe_monthly"], pr["excess_n"], pr["excess_skew"], pr["excess_kurt"],
                n_tr, var)
            pr["n_trials"] = n_tr
    summary = headline(report)
    ledger.record(split, cid, {"name": scorer.name, "version": scorer.version,
                               "params": scorer.params(), "lookback_days": scorer.lookback_days,
                               "maturation_hours": scorer.maturation_hours},
                  engine, summary, accept_contaminated=accept_contaminated)
    return report


def headline(report: dict) -> dict:
    out = {}
    for h, r in report["horizons"].items():
        if r.get("lift"):
            out[f"lift_p5_{h}y"] = r["lift"]["p5"]
            out[f"lift_p10_{h}y"] = r["lift"]["p10"]
            out[f"lift_loss_{h}y"] = r["lift"]["p_loss"]
            out[f"lift_p5_matched_{h}y"] = r.get("lift_matched", {}).get("p5")
            out[f"lift_loss_matched_{h}y"] = r.get("lift_matched", {}).get("p_loss")
            out[f"perm_p5_{h}y"] = r.get("perm_p5")
    if report.get("portfolio"):
        out["sharpe_monthly"] = report["portfolio"].get("sharpe_monthly")
        out["excess_sharpe_monthly"] = report["portfolio"].get("excess_sharpe_monthly")
        out["dsr"] = report["portfolio"].get("deflated_sharpe")
    return out


def _f(x, pct=False, nd=2):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "   -  "
    return f"{x:6.1%}" if pct else f"{x:6.{nd}f}"


def format_report(report: dict) -> str:
    lines = [f"== {report['scorer']}  split={report['split']}  months={report['months']}  "
             f"config={report['config_id']}"]
    for h, r in report["horizons"].items():
        if not r.get("lift"):
            lines.append(f"  {h}y: no result ({r.get('n_candidates', 0)} candidates)")
            continue
        t, u = r["top"], r["universe"]
        ci = r.get("lift_ci", {})
        ci5 = f"[{_f(ci['p5'][0]).strip()},{_f(ci['p5'][1]).strip()}]" if ci.get("p5") else ""
        lines.append(
            f"  {h}y  selected {r['n_selected']:>6,} ({r['unique_selected']:,} names)  "
            f"P5x {_f(t['p5'], True)} vs {_f(u['p5'], True)} univ  lift5 {_f(r['lift']['p5'])} {ci5}  "
            f"(size-matched {_f(r.get('lift_matched', {}).get('p5'))}; "
            f"vs mentioned {_f(r.get('lift_vs_mentioned', {}).get('p5'))})  "
            f"lift10 {_f(r['lift']['p10'])}  ploss lift {_f(r['lift']['p_loss'])} "
            f"(size-matched {_f(r.get('lift_matched', {}).get('p_loss'))})  "
            f"10x recall {_f(r['recall10'], True)}  perm p {_f(r.get('perm_p5'), nd=3)}  "
            f"med excess {_f(t['excess'][50], True)}  KM5x {_f(r['km_p5']['top'], True)}  "
            f"[bad_data excluded: {t.get('bad_data', 0)} top / {u.get('bad_data', 0)} univ]")
    p = report.get("portfolio")
    if p:
        lines.append(f"  portfolio: CAGR {_f(p['cagr'], True)} vs SPY {_f(p['bench_cagr'], True)}  "
                     f"IR {_f(p['info_ratio'])}  Sharpe {_f(p['sharpe'])}  maxDD {_f(p['max_dd'], True)}  "
                     f"top5 gains {_f(p['top5_share_of_gains'], True)}  DSR {_f(p.get('deflated_sharpe'))} "
                     f"({p.get('n_trials')} trials)")
    return "\n".join(lines)


def load_scorer(spec: str) -> Scorer:
    """``path/to/file.py:factory`` or ``package.module:factory``; the factory is called with
    no arguments and must return an object satisfying the Scorer protocol (scorer.py)."""
    import importlib
    import importlib.util

    target, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"--scorer needs module:factory, got {spec!r}")
    if target.endswith(".py"):
        mod_spec = importlib.util.spec_from_file_location(Path(target).stem, target)
        mod = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = mod
        mod_spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(target)
    scorer = getattr(mod, attr)()
    if not isinstance(scorer, Scorer):
        raise SystemExit(f"{spec} did not return a Scorer (needs name, version, lookback_days, "
                         "maturation_hours, params(), score(cohort))")
    return scorer


def make_corpus(scorer, parquet: Path, mentions: Path, post_features: Path):
    from pit import PITConfig, PITCorpus
    return PITCorpus(parquet, mentions, PITConfig(maturation_hours=scorer.maturation_hours),
                     post_features=post_features)


def main(argv=None) -> int:
    from baselines import Baseline

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--features", default="data/features")
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--eodhd", default="data/market/eodhd")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "ladder"):
        s = sub.add_parser(name, help="evaluate one baseline" if name == "run" else "evaluate every baseline")
        s.add_argument("--split", default="dev", choices=list(lg.SPLITS))
        s.add_argument("--lookback", type=int, default=90)
        s.add_argument("--maturation", type=int, default=24)
        s.add_argument("--top", type=float, default=0.1)
        s.add_argument("--seed", type=int, default=0)
        s.add_argument("--size-q", type=int, nargs="+")
        s.add_argument("--min-posts", type=int, default=1)
        s.add_argument("--n-perm", type=int, default=300)
        s.add_argument("--dedupe-months", type=int, default=0,
                       help="skip a company already selected in the previous N cohort months")
        s.add_argument("--accept-contaminated", action="store_true")
        if name == "run":
            g = s.add_mutually_exclusive_group(required=True)
            g.add_argument("--baseline", choices=Baseline.KINDS)
            g.add_argument("--scorer", metavar="MODULE:FACTORY",
                           help="a custom scorer, e.g. my_scorer.py:make_scorer")
    a = p.parse_args(argv)
    feats = Path(a.features)
    ev = Evaluator(feats, sic=Path("data/ref/sic.parquet"))
    store = px.PriceStore(Path(a.eodhd))
    led = lg.Ledger()
    if a.cmd == "run" and a.scorer:
        scorers = [load_scorer(a.scorer)]
    else:
        kinds = [a.baseline] if a.cmd == "run" else list(Baseline.KINDS)
        scorers = [Baseline(k, lookback_days=a.lookback, maturation_hours=a.maturation,
                            min_posts=a.min_posts, seed=a.seed) for k in kinds]
    for sc in scorers:
        corpus = make_corpus(sc, Path(a.parquet), feats / "mentions.parquet", feats / "post_features.parquet")
        rep = run(sc, ev, corpus, a.split, ledger=led, top_frac=a.top, seed=a.seed,
                  size_q=a.size_q, n_perm=a.n_perm, store=store, dedupe_months=a.dedupe_months,
                  accept_contaminated=a.accept_contaminated, log=lambda m: None)
        print(format_report(rep), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
