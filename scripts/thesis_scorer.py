"""The thesis scorer: LLM grades per thread -> signed contribution -> company score over time.

Pipeline (each step is a job; see jobs.py):

    thesis_funnel.py plan        -> data/features/threads.parquet           (which threads)
    thesis_llm.py run / collect  -> data/features/thesis_threads_<variant>.parquet (the grades)
    this module as a Scorer      -> backtest.py run --scorer scripts/thesis_scorer.py:factory
    this module `timeline`       -> data/features/thesis_series_<variant>.parquet (for graphs)
    this module `calibrate`      -> outcome rates per score decile on a split

A thread's quality ``Q`` (0-100) is a weighted sum of the model's grades (argument,
evidence, tone, falsifiability, balance, comment discussion) minus a hype penalty. Its
signed contribution is ``x = stance * (Q/100)^2 * confidence * (1 + log(1 + votes))^gamma``.
A company's score at a cutoff is the sum of its threads' contributions decayed with a
half-life set by each thread's stated horizon, over the threads the point-in-time view
shows at that cutoff.

The weights were tuned on the user's 30 gold ratings (2026-10-22; never on backtest numbers):
``.25 argument + .15 evidence + .05 tone + .05 falsifiability + .20 balance + .30 discussion
- 10 * hype``, where ``discussion = (independent_support + independent_opposition +
strongest_objection_severity) / 3``. Direction lives in ``stance``, so the comment term
measures how much substantive, independent discussion the thread drew regardless of which
way it cut; the user rewards a well-argued rebuttal as much as support. Specificity is not
in Q (it tracked the ratings at about zero). The thesis gate is off by default: a question
or discussion post with good comments still counts, and its small stance keeps the
contribution small. Rank agreement with the ratings rose from 0.43 to 0.72 (Jev) and 0.21 to
0.46 (gpt-5-nano) against the first formula; the optimum is flat over nearby weights.
Optional, each a ledger parameter:

* ``beta``: the price-since-thesis discount ``(P_thesis / P_now)^beta`` (capped at
  ``discount_cap``), reading prices strictly at or before the cutoff;
* ``use_author``: multiply by a causal author weight, the author's mean Q over *earlier*
  threads shrunk toward the running population mean;
* ``min_evidence``: a company is a candidate only if a visible thread's evidence grade
  reaches it;
* ``use_comments``: whether the discussion term is live;
* ``corroboration``: ``discussion`` (above) or ``net`` (the first formula's support minus
  opposition);
* ``thesis_gate``: ``none`` (default), ``hard`` (a non-thesis contributes nothing) or
  ``soft`` (multiply by the model's thesis probability where it gave one);
* ``use_specificity``: average specificity into the argument grade;
* ``quality``: ``additive`` (default, the gold-tuned sum above) or ``gated`` (the user's
  rules of 2026-10-22, see ``gated_quality``: the poster's substance is discounted by
  ``gate_floor`` until independent discussion tests it, ``op_weight`` splits the score
  between the gated post and the discussion's own value, ``discussion_agg`` is mean or max);
* ``direction``: ``signed`` (default; a bear thesis pushes the score down), ``abs`` (the
  stance's size without its sign: conviction either way counts for the company) or ``none``
  (every thread counts as if stance were 1: quality and discussion only);
* ``flat_quality``: Q = 100 and confidence = 1 for every thread, so the score is only the
  vote-weighted, decayed count of graded threads: the funnel without the grades. Together
  with ``direction = none`` it is the "does the LLM add anything" control. These two were
  added after the full dev calibration table showed the lowest score decile (bear theses)
  with the highest 5x rate, so they are backtest-informed: the ledger counts them as trials.

Everything evaluative is deterministic given the grades file, so every ablation that only
changes parameters costs nothing. ``params()`` carries the model, prompt version, variant
and every parameter into the ledger.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices as px  # noqa: E402
from scorer import Cohort  # noqa: E402
from thesis_prompt import MATURATION_HOURS, THESIS_PIT  # noqa: E402

DAY = 86400
WEIGHTS = {"argument": 0.25, "evidence": 0.15, "tone": 0.05, "falsifiability": 0.05,
           "balance": 0.20, "corroboration": 0.30}
# The gated formula (user's rules, 2026-10-22): the poster's own substance ("informed, level,
# bias": argument and evidence, tone, balance and hype), discounted until independent
# discussion corroborates or tests it, plus the discussion's own value. See thread_quality.
OP_WEIGHTS = {"argument": 0.45, "evidence": 0.25, "tone": 0.10, "falsifiability": 0.10, "balance": 0.10}
WEIGHTS_V1 = {"argument": 0.28, "evidence": 0.22, "tone": 0.12, "falsifiability": 0.10,
              "balance": 0.10, "corroboration": 0.18}   # the first formula, for the record
HALF_LIFE_DAYS = {"days": 14, "weeks": 30, "months": 90, "years": 180, "unclear": 60}
SERIES_SCHEMA = pa.schema([("cik", pa.string()), ("ticker", pa.string()), ("ts", pa.int64()),
                           ("post_id", pa.string()), ("x", pa.float64()), ("score", pa.float64())])


@dataclass
class Params:
    gamma: float = 0.5
    beta: float = 0.0
    discount_cap: float = 1.0
    use_comments: bool = True
    use_author: bool = False
    author_k: float = 5.0
    min_evidence: int = 0
    exclude_late: bool = False   # drop threads whose graded text holds content written after
                                 # their first decision cutoff (a comment shown, or a late edit)
    hype_penalty: float = 10.0
    corroboration: str = "discussion"   # discussion | net
    thesis_gate: str = "none"           # none | hard | soft
    use_specificity: bool = False
    direction: str = "signed"           # signed | abs | none
    flat_quality: bool = False          # Q = 100, confidence = 1: the funnel-only control
    quality: str = "additive"           # additive (the gold-tuned sum) | gated (the user's rules)
    gate_floor: float = 0.5             # gated: share of OP substance that counts with no discussion
    op_weight: float = 0.6              # gated: weight of the gated OP term; the rest is the discussion's own value
    discussion_agg: str = "mean"        # gated: mean | max of support, opposition, strongest objection
    weights: dict = field(default_factory=lambda: dict(WEIGHTS))
    half_life: dict = field(default_factory=lambda: dict(HALF_LIFE_DAYS))

    @classmethod
    def from_json(cls, text: str | None) -> "Params":
        p = cls()
        for k, v in (json.loads(text) if text else {}).items():
            if not hasattr(p, k):
                raise SystemExit(f"unknown thesis parameter {k!r}")
            if isinstance(getattr(p, k), dict):
                getattr(p, k).update(v)
            else:
                setattr(p, k, v)
        return p


# ------------------------------------------------------------------ one thread

def corroboration(g: dict, mode: str = "discussion") -> float:
    """The comment term, 0..100. ``discussion``: how much substantive independent discussion
    the thread drew, whichever way it cut (mean of support, opposition and the strongest
    objection's severity). ``net``: support minus opposition mapped to 0..100 (50 = none)."""
    sup = float(g.get("independent_support") or 0)
    opp = float(g.get("independent_opposition") or 0)
    if mode == "net":
        return (1 + (sup - opp) / 100) / 2 * 100
    obj = float(g.get("strongest_objection_severity") or 0)
    return (sup + opp + obj) / 3


def thesis_factor(g: dict, p: Params) -> float:
    """1 for a thesis. A non-thesis: 0 under the hard gate, the model's thesis probability
    under the soft gate (0.5 when the model gave none), 1 with no gate."""
    if p.thesis_gate == "none" or g.get("is_thesis", True):
        return 1.0
    if p.thesis_gate == "hard":
        return 0.0
    prob = g.get("is_thesis_p")
    if prob is None:
        prob = (g.get("_jev") or {}).get("is_thesis_p")
    return float(prob) if prob is not None else 0.5


def op_substance(g: dict, p: Params) -> float:
    """The poster's own substance, 0..100: informed (argument, evidence), level (tone),
    checkable (falsifiability), fair (balance), minus hype. No comment grade enters."""
    arg = float(g.get("argument_quality") or 0)
    if p.use_specificity:
        arg = (arg + float(g.get("specificity") or 0)) / 2
    parts = {"argument": arg, "evidence": float(g.get("evidence_quality") or 0),
             "tone": float(g.get("tone_stability") or 0), "falsifiability": float(g.get("falsifiability") or 0),
             "balance": float(g.get("balance") or 0)}
    q = sum(OP_WEIGHTS[k] * parts[k] for k in OP_WEIGHTS) - p.hype_penalty * float(g.get("hype_risk") or 0) / 100
    return max(0.0, min(100.0, q))


def discussion_value(g: dict, agg: str = "mean") -> float:
    """How much substantive independent discussion the thread drew, 0..100, whichever way it
    cut: the mean (or the max) of independent support, independent opposition and the
    strongest objection's severity."""
    parts = [float(g.get(k) or 0) for k in ("independent_support", "independent_opposition",
                                            "strongest_objection_severity")]
    return max(parts) if agg == "max" else sum(parts) / 3


def gated_quality(g: dict, p: Params) -> float:
    """The user's rule: the poster came in with a goal, so the post's substance only counts
    in full once independent commenters have tested it; the discussion itself is worth
    something even under a thin post.  Q = 100 * (op_weight * OP * (floor + (1 - floor) * D)
    + (1 - op_weight) * D), OP and D in 0..1.  With no discussion a perfect write-up scores
    100 * op_weight * floor (30 at the defaults, "a 5 at best"); a great top comment under a
    thin post scores about 100 * (1 - op_weight) (40 to 50)."""
    op = op_substance(g, p) / 100
    d = (discussion_value(g, p.discussion_agg) if p.use_comments else 0.0) / 100
    q = 100 * (p.op_weight * op * (p.gate_floor + (1 - p.gate_floor) * d) + (1 - p.op_weight) * d)
    return max(0.0, min(100.0, q))


def thread_quality(g: dict, p: Params) -> float:
    """Q in 0..100 for one grade record (flat or nested ``comments``)."""
    if "comments" in g and isinstance(g["comments"], dict):
        g = {**g, **g["comments"]}
    if p.quality == "gated":
        return gated_quality(g, p) * thesis_factor(g, p)
    if p.quality != "additive":
        raise ValueError(f"quality must be additive or gated, not {p.quality!r}")
    arg = float(g.get("argument_quality") or 0)
    if p.use_specificity:
        arg = (arg + float(g.get("specificity") or 0)) / 2
    neutral = 50.0 if p.corroboration == "net" else 0.0
    parts = {"argument": arg, "evidence": float(g.get("evidence_quality") or 0),
             "tone": float(g.get("tone_stability") or 0),
             "falsifiability": float(g.get("falsifiability") or 0),
             "balance": float(g.get("balance") or 0),
             "corroboration": corroboration(g, p.corroboration) if p.use_comments else neutral}
    q = sum(p.weights[k] * parts[k] for k in p.weights)
    q -= p.hype_penalty * float(g.get("hype_risk") or 0) / 100
    return max(0.0, min(100.0, q)) * thesis_factor(g, p)


def is_late(g: dict) -> bool:
    """True if the model saw something a reader at the thread's first cutoff could not have."""
    return bool(g.get("late_shown") or 0) or bool(g.get("edited_late"))


def vote_weight(votes, gamma: float) -> float:
    return (1 + math.log1p(max(float(votes or 0), 0.0))) ** gamma


def contribution(g: dict, p: Params) -> float:
    stance = float(g.get("stance") or 0)
    if p.direction == "abs":
        stance = abs(stance)
    elif p.direction == "none":
        stance = 1.0
    elif p.direction != "signed":
        raise ValueError(f"direction must be signed, abs or none, not {p.direction!r}")
    if p.flat_quality:
        q, conf = 100.0, 1.0
    else:
        q, conf = thread_quality(g, p), float(g.get("confidence") or 0)
    return stance * (q / 100) ** 2 * conf * vote_weight(g.get("votes"), p.gamma)


def decay(dt_days: float, horizon: str | None, p: Params) -> float:
    h = p.half_life.get(horizon or "unclear", p.half_life["unclear"])
    return 2.0 ** (-max(dt_days, 0.0) / h)


# ------------------------------------------------------------------ author credibility (causal)

def author_priors(rows: list[dict], p: Params) -> dict[tuple, float]:
    """For every thread, the author's credibility *before* it: mean Q of the author's earlier
    threads shrunk toward the running mean of all earlier threads (k = author_k). Threads
    are processed in maturation order, so nothing later can leak in. Returns 0..1."""
    out: dict[tuple, float] = {}
    by_author: dict[str, list[float]] = {}
    pop_sum = pop_n = 0.0
    for r in sorted(rows, key=lambda r: (r["matured_utc"], r["post_id"])):
        q = thread_quality(r, p)
        a = r.get("author") or ""
        hist = by_author.get(a, [])
        pop_mean = pop_sum / pop_n if pop_n else 50.0
        n = len(hist)
        prior = (n / (n + p.author_k)) * (sum(hist) / n if n else 0) + (p.author_k / (n + p.author_k)) * pop_mean
        out[(r["post_id"], r["cik"])] = prior / 100
        by_author.setdefault(a, []).append(q)
        pop_sum += q
        pop_n += 1
    return out


# ------------------------------------------------------------------ price discount

def price_ratio(store: px.PriceStore, code: str | None, day_thesis: int, day_cutoff: int) -> float | None:
    """P_now / P_thesis from adjusted closes at or before each day; None if unavailable.
    Refuses a cutoff before the thesis day: that would read the future."""
    if day_cutoff < day_thesis:
        raise ValueError("cutoff before the thesis day")
    s = store.get(code)
    if s is None:
        return None
    i0, i1 = s.index_on_or_before(day_thesis), s.index_on_or_before(day_cutoff)
    if i0 is None or i1 is None or s.days[i1] > day_cutoff:
        return None
    p0, p1 = float(s.close[i0]), float(s.close[i1])
    return p1 / p0 if p0 > 0 and p1 > 0 else None


def discount(ratio: float | None, p: Params) -> float:
    if ratio is None or p.beta == 0:
        return 1.0
    return min(p.discount_cap, ratio ** (-p.beta))


# ------------------------------------------------------------------ the scorer

class ThesisScorer:
    name = "thesis"
    version = "1"

    def __init__(self, grades: list[dict], params: Params | None = None, *, variant: str = "identified",
                 votes: str = "final", lookback_days: int = 90, maturation_hours: int = MATURATION_HOURS,
                 store: px.PriceStore | None = None, eodhd_root: Path = Path("data/market/eodhd")):
        self.p = params or Params()
        self.variant, self.votes = variant, votes
        self.lookback_days, self.maturation_hours = lookback_days, maturation_hours
        self.edited_policy, self.comments = THESIS_PIT["edited_policy"], THESIS_PIT["comments"]
        if self.p.exclude_late:
            grades = [g for g in grades if not is_late(g)]
        self.grades = {(g["post_id"], g["cik"]): g for g in grades}
        self.model = grades[0].get("model") if grades else None
        self.prompt_version = grades[0].get("prompt_version") if grades else None
        self._priors = author_priors(grades, self.p) if self.p.use_author else {}
        self._store, self._eodhd = store, Path(eodhd_root)
        self.name = f"thesis:{variant}"

    @property
    def store(self) -> px.PriceStore:
        if self._store is None:
            self._store = px.PriceStore(self._eodhd)
        return self._store

    def params(self) -> dict:
        return {"model": self.model, "prompt_version": self.prompt_version, "variant": self.variant,
                "votes": self.votes, "n_graded": len(self.grades), "maturation_hours": self.maturation_hours,
                "pit": dict(THESIS_PIT), **asdict(self.p)}

    def thread_value(self, g: dict, cutoff_utc: int, code: str | None) -> float:
        x = contribution(g, self.p)
        if x == 0:
            return 0.0
        x *= decay((cutoff_utc - g["matured_utc"]) / DAY, g.get("horizon"), self.p)
        if self.p.use_author:
            x *= 0.5 + 0.5 * self._priors.get((g["post_id"], g["cik"]), 0.5)
        if self.p.beta:
            x *= discount(price_ratio(self.store, code, g["matured_utc"] // DAY, cutoff_utc // DAY), self.p)
        return x

    def score(self, cohort: Cohort) -> dict[str, dict[str, float]]:
        seen, acc = set(), {}
        for r in cohort.view.posts():
            key = (r["post_id"], r["cik"])
            if key in seen or key not in self.grades or r["cik"] not in cohort.universe:
                continue
            seen.add(key)
            g = self.grades[key]
            if g["matured_utc"] > cohort.cutoff_utc:
                continue   # the view should never show this; belt and braces
            a = acc.setdefault(r["cik"], {"score": 0.0, "n_threads": 0, "best_q": 0.0,
                                          "best_evidence": 0, "net_stance": 0.0})
            a["score"] += self.thread_value(g, cohort.cutoff_utc, cohort.universe[r["cik"]].get("code"))
            a["n_threads"] += 1
            a["best_q"] = max(a["best_q"], thread_quality(g, self.p))
            a["best_evidence"] = max(a["best_evidence"], int(g.get("evidence_quality") or 0))
            a["net_stance"] += float(g.get("stance") or 0)
        if self.p.min_evidence:
            acc = {c: a for c, a in acc.items() if a["best_evidence"] >= self.p.min_evidence}
        return acc


# ------------------------------------------------------------------ the series for graphs

def timeline(grades: list[dict], p: Params) -> list[dict]:
    """One row per thread event, in time order, with the company's decayed score right after
    it. No price discount here (it needs a cutoff); author weight applies if enabled."""
    if p.exclude_late:
        grades = [g for g in grades if not is_late(g)]
    priors = author_priors(grades, p) if p.use_author else {}
    events: dict[str, list[tuple[int, float, str]]] = {}
    out = []
    for g in sorted(grades, key=lambda g: (g["matured_utc"], g["post_id"])):
        x = contribution(g, p)
        if p.use_author and x:
            x *= 0.5 + 0.5 * priors.get((g["post_id"], g["cik"]), 0.5)
        hist = events.setdefault(g["cik"], [])
        hist.append((g["matured_utc"], x, g.get("horizon") or "unclear"))
        t = g["matured_utc"]
        s = sum(xj * decay((t - tj) / DAY, hj, p) for tj, xj, hj in hist)
        out.append({"cik": g["cik"], "ticker": g.get("ticker"), "ts": t, "post_id": g["post_id"],
                    "x": x, "score": s})
    return out


# ------------------------------------------------------------------ calibration

def calibrate(scores_by_decile: dict[int, dict]) -> str:
    """One line per decile: the multibagger and ruin rates, how often a pick beat the market
    over the horizon and by how much on average, the CAGR quartiles, and the picks' average
    return over the 12 months *before* the cutoff (prior 12m), which says whether a decile
    is catching companies that had already run."""
    lines = ["decile  n     P(5x)  P(10x) P(loss) P(beat mkt) mean excess  median CAGR  25% CAGR  75% CAGR  prior 12m"]
    for d in sorted(scores_by_decile, reverse=True):
        r = scores_by_decile[d]
        if not r.get("n"):
            lines.append(f"{d:6d}  {0:5d}")
            continue
        c = r["cagr"]
        mom = r.get("mom", float("nan"))
        lines.append(f"{d:6d}  {r['n']:5d}  {r['p5']:.3f}  {r['p10']:.3f}  {r['p_loss']:.3f}  "
                     f"{r.get('p_beat', float('nan')):11.3f}  {r.get('mean_excess', float('nan')):11.1%}  "
                     f"{c[50]:10.1%}  {c[25]:8.1%}  {c[75]:8.1%}  {mom:9.1%}")
    return "\n".join(lines)


def deciles_within_month(months, scores) -> np.ndarray:
    """Score decile of each candidate within its month, 1 = lowest, 10 = highest."""
    months, scores = np.asarray(months), np.asarray(scores, dtype=float)
    dec = np.zeros(len(scores), dtype=int)
    for m in np.unique(months):
        idx = np.nonzero(months == m)[0]
        order = scores[idx].argsort()
        ranks = np.empty(len(idx))
        ranks[order] = np.arange(len(idx))
        dec[idx] = np.minimum(10, (ranks / max(len(idx), 1) * 10).astype(int) + 1)
    return dec


def decile_rates(cand: dict) -> dict[int, dict]:
    """Within-month score deciles (10 = best) pooled over months, with the engine's rates and
    the decile's mean prior-12-month return when the candidates carry ``mom``."""
    from backtest import event_rates
    dec = deciles_within_month(cand["month"], cand["score"])
    out = {}
    for d in range(1, 11):
        sub = {k: v[dec == d] for k, v in cand.items()}
        out[d] = event_rates(sub)
        if "mom" in sub and len(sub["mom"]):
            mom = np.asarray(sub["mom"], dtype=float)
            out[d]["mom"] = float(np.nanmean(mom)) if np.isfinite(mom).any() else float("nan")
    return out


def _day(ts: int | float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def sample_report(cand: dict, grades: list[dict], p: Params, titles: dict[str, str], *, years: int,
                  n: int = 6, lookback_days: int = 90, deciles=(1, 10)) -> str:
    """The most extreme company-months of the bottom and top deciles, with every graded thread
    the scorer could see at that cutoff (written inside the lookback, before the cutoff), its
    stance, quality and signed contribution, and the outcome measured from the cutoff forward.
    A reader can check the point-in-time ordering line by line: post date < cutoff < entry."""
    from backtest import month_utc
    dec = deciles_within_month(cand["month"], cand["score"])
    by_cik: dict[str, list[dict]] = {}
    for g in grades:
        by_cik.setdefault(g["cik"], []).append(g)
    lines = []
    for d in deciles:
        idx = np.nonzero(dec == d)[0]
        if not len(idx):
            continue
        order = idx[np.argsort(np.asarray(cand["score"], dtype=float)[idx])]
        pick = order[:n] if d == 1 else order[::-1][:n]
        lines.append(f"\n== decile {d} ({'lowest' if d == 1 else 'highest'} scores), {len(idx):,} company-months, "
                     f"{n} most extreme, {years}y outcomes ==")
        for i in pick:
            cutoff = month_utc(str(cand["month"][i]))
            mom = float(cand["mom"][i]) if "mom" in cand else float("nan")
            lines.append(f"{cand['ticker'][i]} cohort {cand['month'][i]} (cutoff {_day(cutoff)}, entry = first open after it)"
                         f"  score {float(cand['score'][i]):+.3f}  prior 12m {mom:+.0%}  status {cand['status'][i]}"
                         f"  {years}y max x{float(cand['max_mult'][i]):.1f}  excess vs market {float(cand['excess'][i]):+.0%}"
                         f"  permanent loss {bool(cand['perm_loss'][i])}")
            vis = [g for g in by_cik.get(str(cand["cik"][i]), [])
                   if cutoff - lookback_days * DAY <= g["matured_utc"] <= cutoff]
            for g in sorted(vis, key=lambda g: contribution(g, p)):
                lines.append(f"    {_day(g['matured_utc'])}  stance {float(g.get('stance') or 0):+.1f}"
                             f"  Q {thread_quality(g, p):5.1f}  x {contribution(g, p):+.3f}"
                             f"  votes {int(g.get('votes') or 0):4d}  {(titles.get(g['post_id']) or '')[:90]}")
    return "\n".join(lines)


# ------------------------------------------------------------------ loading and CLI

def load_grades(features: Path, variant: str) -> list[dict]:
    path = Path(features) / f"thesis_threads_{variant}.parquet"
    if not path.exists():
        raise SystemExit(f"{path} is missing: run the llm-run and llm-collect jobs for {variant}")
    return pq.read_table(path).to_pylist()


def factory(env: dict | None = None) -> ThesisScorer:
    """Built from the environment so ``backtest.py run --scorer scripts/thesis_scorer.py:factory``
    needs no code change per experiment: THESIS_VARIANT, THESIS_PARAMS (JSON), THESIS_VOTES,
    THESIS_LOOKBACK, THESIS_FEATURES."""
    env = dict(os.environ if env is None else env)
    variant = env.get("THESIS_VARIANT", "identified")
    grades = load_grades(Path(env.get("THESIS_FEATURES", "data/features")), variant)
    return ThesisScorer(grades, Params.from_json(env.get("THESIS_PARAMS")), variant=variant,
                        votes=env.get("THESIS_VOTES", "final"),
                        lookback_days=int(env.get("THESIS_LOOKBACK", 90)),
                        eodhd_root=Path(env.get("THESIS_EODHD", "data/market/eodhd")))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--features", default="data/features")
    p.add_argument("--variant", default="identified")
    p.add_argument("--params", default=os.environ.get("THESIS_PARAMS"), help="JSON overrides")
    p.add_argument("--exclude-late", action="store_true",
                   help="drop threads holding content written after their first cutoff "
                        "(same as THESIS_PARAMS {\"exclude_late\": true})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("timeline", help="write thesis_series_<variant>.parquet")
    c = sub.add_parser("calibrate", help="outcome rates per score decile on a split")
    c.add_argument("--split", default="dev")
    c.add_argument("--years", type=int, nargs="+", default=[5, 3])
    c.add_argument("--parquet", default="data/corpus/parquet")
    c.add_argument("--mentions", default="data/features/mentions.parquet")
    c.add_argument("--post-features", default="data/features/post_features.parquet")
    c.add_argument("--samples", type=int, default=0,
                   help="print the N most extreme company-months of the bottom and top deciles "
                        "with the threads behind them (a point-in-time sanity check)")
    a = p.parse_args(argv)
    params = Params.from_json(a.params)
    if a.exclude_late:
        params.exclude_late = True
    grades = load_grades(Path(a.features), a.variant)
    if a.cmd == "timeline":
        rows = timeline(grades, params)
        dest = Path(a.features) / f"thesis_series_{a.variant}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=SERIES_SCHEMA), dest, compression="zstd")
        print(f"wrote {len(rows):,} events for {len({r['cik'] for r in rows}):,} companies to {dest}")
        return 0
    import backtest as bt
    import ledger as lg
    scorer = ThesisScorer(grades, params, variant=a.variant)
    ev = bt.Evaluator(Path(a.features), sic=Path("data/ref/sic.parquet"))
    corpus = bt.make_corpus(scorer, Path(a.parquet), Path(a.mentions), Path(a.post_features))
    months = lg.split_months(a.split, ev.months())
    if a.split == lg.SEALED:
        raise SystemExit("calibrate does not touch the sealed holdout")
    stamp = bt.data_stamp(Path(a.mentions), Path(a.post_features), Path(a.features) / "universe.parquet")
    late = sum(1 for g in grades if is_late(g))
    print(f"{len(grades):,} graded threads, {late:,} hold content written after their first cutoff; "
          f"exclude_late={params.exclude_late}")
    scores = bt.build_scores(scorer, months, corpus, ev.con, Path("data/experiments"), print, stamp)
    titles: dict[str, str] = {}
    if a.samples:
        ids = sorted({g["post_id"] for g in grades})
        corpus.con.register("wanted", pa.table({"id": pa.array(ids)}))
        titles = {r[0]: r[1] for r in corpus.con.execute(
            "select p.id, p.title from posts p join wanted w on w.id = p.id").fetchall()}
    for years in a.years:
        cand = ev.joined(scores, years, months)
        print(f"\n{a.split}, {years}y horizon, {len(cand['month']):,} candidate-months, "
              f"params {json.dumps(asdict(params))}")
        print(calibrate(decile_rates(cand)))
        if a.samples:
            print(sample_report(cand, list(scorer.grades.values()), params, titles, years=years,
                                n=a.samples, lookback_days=scorer.lookback_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
