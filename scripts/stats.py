"""Statistics for long-horizon, overlapping, right-censored outcomes.

* ``km_event_prob``    - Kaplan-Meier probability that an event (e.g. a 5x) has happened by t,
                         counting censored holdings correctly instead of as failures.
* ``block_bootstrap``  - resample calendar blocks (not rows): cohorts a month apart share most
                         of their forward window, so they are not independent observations.
* ``permutation_p``    - shuffle scores within strata (month x size bucket): does ranking beat
                         what mere exposure to that kind of stock delivers?
* ``deflated_sharpe``  - Bailey & Lopez de Prado: the probability the best of N tried
                         configurations has a truly positive Sharpe.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
EULER = 0.5772156649015329


def km_event_prob(durations, events, t: float) -> float:
    """P(event by time t) from right-censored data. ``events`` is 1 when the event happened
    at ``durations``, 0 when the holding was only observed that long."""
    d = np.asarray(durations, dtype=float)
    e = np.asarray(events, dtype=bool)
    if len(d) == 0:
        return float("nan")
    order = np.argsort(d, kind="stable")
    d, e = d[order], e[order]
    surv, at_risk, i, n = 1.0, len(d), 0, len(d)
    while i < n and d[i] <= t:
        j = i
        while j < n and d[j] == d[i]:
            j += 1
        events_here = int(e[i:j].sum())
        if events_here:
            surv *= 1 - events_here / at_risk
        at_risk -= j - i
        i = j
    return 1 - surv


def block_bootstrap(blocks: dict, stat, n: int = 1000, seed: int = 0) -> np.ndarray:
    """``blocks`` maps a block label (e.g. '2015Q3') to that block's rows; ``stat`` takes the
    concatenated rows of a resample and returns a number (or nan). Returns the n draws."""
    rng = np.random.default_rng(seed)
    keys = list(blocks)
    out = np.empty(n)
    for b in range(n):
        pick = rng.integers(0, len(keys), len(keys))
        out[b] = stat([blocks[keys[k]] for k in pick])
    return out


def percentile_ci(draws: np.ndarray, level: float = 0.95) -> tuple[float, float]:
    d = draws[np.isfinite(draws)]
    if len(d) == 0:
        return (float("nan"), float("nan"))
    a = (1 - level) / 2
    return float(np.quantile(d, a)), float(np.quantile(d, 1 - a))


def permutation_p(strata: list[tuple], stat, observed: float, n: int = 1000, seed: int = 0) -> float:
    """One-sided p: share of permutations whose statistic is >= observed. ``strata`` is a
    list of (scores, outcomes) array pairs (one per month x bucket); each permutation
    shuffles the outcomes against the scores inside every stratum, then ``stat`` is applied
    to the list of shuffled pairs."""
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n):
        shuffled = [(s, rng.permutation(o)) for s, o in strata]
        if stat(shuffled) >= observed:
            hits += 1
    return (hits + 1) / (n + 1)


def sharpe_moments(returns) -> tuple[float, float, float, int]:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 3 or r.std(ddof=1) == 0:
        return float("nan"), float("nan"), float("nan"), n
    sd = r.std(ddof=1)
    z = (r - r.mean()) / sd
    return float(r.mean() / sd), float((z ** 3).mean()), float((z ** 4).mean()), n


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max Sharpe] among n_trials independent zero-skill configurations."""
    if n_trials <= 1:
        return 0.0
    g = EULER
    return math.sqrt(var_sr) * ((1 - g) * _N.inv_cdf(1 - 1 / n_trials)
                                + g * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, n_trials: int,
                    var_trial_sr: float) -> float:
    """Probability that the true Sharpe exceeds what the best of ``n_trials`` zero-skill
    configurations would show by luck. ``sr`` and ``var_trial_sr`` are per-period (not annualised)."""
    if not all(np.isfinite([sr, skew, kurt])) or n_obs < 3:
        return float("nan")
    sr0 = expected_max_sharpe(n_trials, var_trial_sr)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return float("nan")
    return _N.cdf((sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def quantiles(values, qs=(10, 25, 50, 75, 90)) -> dict:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return {q: float("nan") for q in qs}
    return {q: float(np.percentile(v, q)) for q in qs}
