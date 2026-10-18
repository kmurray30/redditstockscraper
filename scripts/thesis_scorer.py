"""The thesis scorer: LLM grades per thread -> signed contribution -> company score over time.

Pipeline (each step is a job; see jobs.py):

    thesis_funnel.py plan        -> data/features/threads.parquet           (which threads)
    thesis_llm.py run / collect  -> data/features/thesis_threads_<variant>.parquet (the grades)
    this module as a Scorer      -> backtest.py run --scorer scripts/thesis_scorer.py:factory
    this module `timeline`       -> data/features/thesis_series_<variant>.parquet (for graphs)
    this module `calibrate`      -> outcome rates per score decile on a split

A thread's quality ``Q`` (0-100) is a weighted sum of the model's grades (argument,
evidence, tone, falsifiability, balance, comment corroboration) minus a hype penalty. Its
signed contribution is ``x = stance * (Q/100)^2 * confidence * (1 + log(1 + votes))^gamma``;
non-theses contribute nothing. A company's score at a cutoff is the sum of its threads'
contributions decayed with a half-life set by each thread's stated horizon, over the
threads the point-in-time view shows at that cutoff. Optional, each a ledger parameter:

* ``beta``: the price-since-thesis discount ``(P_thesis / P_now)^beta`` (capped at
  ``discount_cap``), reading prices strictly at or before the cutoff;
* ``use_author``: multiply by a causal author weight, the author's mean Q over *earlier*
  threads shrunk toward the running population mean;
* ``min_evidence``: a company is a candidate only if a visible thread's evidence grade
  reaches it;
* ``use_comments``: whether the corroboration term is live.

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

DAY = 86400
WEIGHTS = {"argument": 0.28, "evidence": 0.22, "tone": 0.12, "falsifiability": 0.10,
           "balance": 0.10, "corroboration": 0.18}
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
    hype_penalty: float = 20.0
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

def corroboration(g: dict) -> float:
    """Net independent corroboration from the comment grades, mapped to 0..100 (50 = none)."""
    sup = float(g.get("independent_support") or 0)
    opp = float(g.get("independent_opposition") or 0)
    return (1 + (sup - opp) / 100) / 2 * 100


def thread_quality(g: dict, p: Params) -> float:
    """Q in 0..100 for one grade record (flat or nested ``comments``)."""
    if "comments" in g and isinstance(g["comments"], dict):
        g = {**g, **g["comments"]}
    if not g.get("is_thesis", True):
        return 0.0
    arg = (float(g.get("argument_quality") or 0) + float(g.get("specificity") or 0)) / 2
    parts = {"argument": arg, "evidence": float(g.get("evidence_quality") or 0),
             "tone": float(g.get("tone_stability") or 0),
             "falsifiability": float(g.get("falsifiability") or 0),
             "balance": float(g.get("balance") or 0),
             "corroboration": corroboration(g) if p.use_comments else 50.0}
    q = sum(p.weights[k] * parts[k] for k in p.weights)
    q -= p.hype_penalty * float(g.get("hype_risk") or 0) / 100
    return max(0.0, min(100.0, q))


def vote_weight(votes, gamma: float) -> float:
    return (1 + math.log1p(max(float(votes or 0), 0.0))) ** gamma


def contribution(g: dict, p: Params) -> float:
    if not g.get("is_thesis", True):
        return 0.0
    q = thread_quality(g, p)
    return float(g.get("stance") or 0) * (q / 100) ** 2 * float(g.get("confidence") or 0) * \
        vote_weight(g.get("votes"), p.gamma)


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
                 votes: str = "final", lookback_days: int = 90, maturation_hours: int = 24,
                 store: px.PriceStore | None = None, eodhd_root: Path = Path("data/market/eodhd")):
        self.p = params or Params()
        self.variant, self.votes = variant, votes
        self.lookback_days, self.maturation_hours = lookback_days, maturation_hours
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
                "votes": self.votes, "n_graded": len(self.grades), **asdict(self.p)}

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
    lines = ["decile  n     P(5x)  P(10x) P(loss) median CAGR  25% CAGR  75% CAGR"]
    for d in sorted(scores_by_decile, reverse=True):
        r = scores_by_decile[d]
        if not r.get("n"):
            lines.append(f"{d:6d}  {0:5d}")
            continue
        c = r["cagr"]
        lines.append(f"{d:6d}  {r['n']:5d}  {r['p5']:.3f}  {r['p10']:.3f}  {r['p_loss']:.3f}  "
                     f"{c[50]:10.1%}  {c[25]:8.1%}  {c[75]:8.1%}")
    return "\n".join(lines)


def decile_rates(cand: dict) -> dict[int, dict]:
    """Within-month score deciles (10 = best) pooled over months, with the engine's rates."""
    from backtest import event_rates
    months, scores = np.asarray(cand["month"]), np.asarray(cand["score"], dtype=float)
    dec = np.zeros(len(scores), dtype=int)
    for m in np.unique(months):
        idx = np.nonzero(months == m)[0]
        order = scores[idx].argsort()
        ranks = np.empty(len(idx))
        ranks[order] = np.arange(len(idx))
        dec[idx] = np.minimum(10, (ranks / max(len(idx), 1) * 10).astype(int) + 1)
    return {d: event_rates({k: v[dec == d] for k, v in cand.items()}) for d in range(1, 11)}


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
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("timeline", help="write thesis_series_<variant>.parquet")
    c = sub.add_parser("calibrate", help="outcome rates per score decile on a split")
    c.add_argument("--split", default="dev")
    c.add_argument("--years", type=int, nargs="+", default=[5, 3])
    c.add_argument("--parquet", default="data/corpus/parquet")
    c.add_argument("--mentions", default="data/features/mentions.parquet")
    c.add_argument("--post-features", default="data/features/post_features.parquet")
    a = p.parse_args(argv)
    params = Params.from_json(a.params)
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
    scores = bt.build_scores(scorer, months, corpus, ev.con, Path("data/experiments"), print, stamp)
    for years in a.years:
        cand = ev.joined(scores, years, months)
        print(f"\n{a.split}, {years}y horizon, {len(cand['month']):,} candidate-months, "
              f"params {json.dumps(asdict(params))}")
        print(calibrate(decile_rates(cand)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
