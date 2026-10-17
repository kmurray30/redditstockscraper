"""Experiment ledger and the sealed-holdout guard.

Every evaluation is appended to ``data/ledger/experiments.jsonl`` (configuration id, scorer,
split, headline numbers, git commit). The count of distinct configurations tried is what
the deflated Sharpe ratio needs ("how many things did we try before this one looked good?").

Splits (cohort months, inclusive):

    dev       2012-01 .. 2018-12   develop and tune
    validate  2019-01 .. 2020-12   choose thresholds / compare finalists
    holdout   2021-01 .. 2022-12   SEALED: one consultation, spans the GME regime shift
    forward   2023-01 .. 2024-12   LLM-hindsight check and early forward test (labels censored)

Labels look 1-10 years ahead, so a dev cohort's outcome window runs into the validate and
holdout calendar periods. When *training or tuning* on one split and testing on a later one,
drop training cohorts within ``embargo_months`` of the test start (see ``embargo``).

The holdout can be evaluated once. A second look raises ``SealedHoldoutError`` unless the
caller explicitly accepts that the holdout is now contaminated, which is recorded forever.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SPLITS = {
    "dev": ("2012-01-01", "2018-12-01"),
    "validate": ("2019-01-01", "2020-12-01"),
    "holdout": ("2021-01-01", "2022-12-01"),
    "forward": ("2023-01-01", "2024-12-01"),
}
SEALED = "holdout"


class SealedHoldoutError(RuntimeError):
    pass


def split_months(name: str, months: list[str]) -> list[str]:
    lo, hi = SPLITS[name]
    return [m for m in months if lo <= m <= hi]


def split_of(month: str) -> str | None:
    return next((n for n, (lo, hi) in SPLITS.items() if lo <= month <= hi), None)


def embargo(train_months: list[str], test_start: str, horizon_years: float) -> list[str]:
    """Training cohorts whose outcome window does not reach into the test period."""
    y, m = int(test_start[:4]), int(test_start[5:7])
    total = y * 12 + (m - 1) - int(round(horizon_years * 12))
    cut = f"{total // 12:04d}-{total % 12 + 1:02d}-01"
    return [t for t in train_months if t <= cut]


def _git_commit() -> str | None:
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA")  # the hosted worker has no .git folder
    if sha:
        return sha[:7]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5, cwd=Path(__file__).resolve().parent)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Ledger:
    def __init__(self, root: Path = Path("data/ledger")):
        self.root = Path(root)
        self.path = self.root / "experiments.jsonl"
        self.holdout_path = self.root / "holdout.json"

    # ------------------------------------------------------------ reading

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def n_trials(self, *, include_holdout: bool = False) -> int:
        """Distinct configurations evaluated (the multiple-testing count)."""
        return len({e["config_id"] for e in self.entries()
                    if include_holdout or e["split"] != SEALED})

    def trial_sharpes(self) -> list[float]:
        """Per-period Sharpe of excess returns over the benchmark for every distinct configuration
        (best entry per config); the spread of these is what the deflated Sharpe corrects for."""
        best: dict[str, float] = {}
        for e in self.entries():
            sr = (e.get("summary") or {}).get("excess_sharpe_monthly")
            if sr is not None and e["split"] != SEALED:
                best[e["config_id"]] = max(sr, best.get(e["config_id"], -1e9))
        return list(best.values())

    def holdout_state(self) -> dict:
        if self.holdout_path.exists():
            return json.loads(self.holdout_path.read_text())
        return {"consulted": [], "contaminated": False}

    # ------------------------------------------------------------ guard + writing

    def check(self, split: str, config_id: str, *, accept_contaminated: bool = False) -> None:
        """Call before evaluating. Raises if this would be a repeat look at the sealed holdout."""
        if split != SEALED:
            return
        state = self.holdout_state()
        if state["consulted"] and not accept_contaminated:
            first = state["consulted"][0]
            raise SealedHoldoutError(
                f"the holdout was already consulted ({first['ts']}, config {first['config_id']}). "
                "A second look makes it a development set. Pass accept_contaminated=True to "
                "proceed anyway; that is recorded permanently, and a new holdout (future data) "
                "is then needed.")

    def record(self, split: str, config_id: str, scorer: dict, engine: dict, summary: dict,
               *, accept_contaminated: bool = False) -> dict:
        self.check(split, config_id, accept_contaminated=accept_contaminated)
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "config_id": config_id, "split": split, "scorer": scorer, "engine": engine,
                 "summary": summary, "git": _git_commit(), "trial_no": self.n_trials() + 1}
        if split == SEALED:
            state = self.holdout_state()
            state["consulted"].append({"ts": entry["ts"], "config_id": config_id})
            state["contaminated"] = len(state["consulted"]) > 1
            self.holdout_path.write_text(json.dumps(state, indent=2))
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry
