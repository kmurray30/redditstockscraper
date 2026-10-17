"""The interface between the data/backtest machinery and anything that scores companies.

The backtest engine builds one ``Cohort`` per month and asks a ``Scorer`` for a score per
company. A scorer sees only the ``CohortView`` (point-in-time by construction, see pit.py)
and the cohort's eligible universe rows; it cannot see labels or later data.

Everything evaluative (LLM thesis scoring, comment adjudication, author credibility,
novelty, event de-duplication, the quality formula) is meant to live behind this interface.
Deterministic baselines are in baselines.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pit import CohortView


@dataclass(frozen=True)
class Cohort:
    month: str                    # cohort date YYYY-MM-DD (the information cutoff, 00:00 UTC)
    cutoff_utc: int
    view: CohortView              # the corpus as of the cutoff, looking back lookback_days
    universe: dict[str, dict]     # cik -> eligible-universe row (ticker, code, price, adv60, mom, size_q)


@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str
    lookback_days: int            # how much Reddit history the engine should expose
    maturation_hours: int         # thread maturation window (the engine builds the PIT view)

    def params(self) -> dict: ...

    def score(self, cohort: Cohort) -> dict[str, dict[str, float]]:
        """cik -> {"score": float, ...extra diagnostics}. A company missing from the result
        has no signal this month and is not a candidate."""
        ...


def config_hash(scorer: Scorer, extra: dict | None = None) -> str:
    """Stable id of a scorer configuration, for the experiment ledger and score cache."""
    blob = json.dumps({"name": scorer.name, "version": scorer.version,
                       "lookback_days": scorer.lookback_days,
                       "maturation_hours": scorer.maturation_hours,
                       "params": scorer.params(), "extra": extra or {}},
                      sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]
