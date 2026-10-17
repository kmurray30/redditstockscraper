import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import ledger as lg  # noqa: E402


def test_split_membership():
    assert lg.split_of("2015-06-01") == "dev" and lg.split_of("2021-03-01") == "holdout"
    assert lg.split_of("2010-01-01") is None
    months = ["2012-01-01", "2018-12-01", "2019-01-01"]
    assert lg.split_months("dev", months) == ["2012-01-01", "2018-12-01"]


def test_embargo_removes_cohorts_whose_outcomes_overlap_the_test():
    months = ["2016-01-01", "2016-12-01", "2017-01-01", "2018-06-01"]
    # 3-year horizon, test starts 2019-01: a cohort from 2016-01 ends right at 2019-01
    kept = lg.embargo(months, "2019-01-01", 3)
    assert kept == ["2016-01-01"]
    # 1-year horizon: only cohorts starting by 2018-01 are clear of 2019-01; 2018-06 is not
    assert lg.embargo(months, "2019-01-01", 1) == ["2016-01-01", "2016-12-01", "2017-01-01"]


def make(tmp_path):
    return lg.Ledger(tmp_path / "ledger")


def test_records_and_counts_distinct_configs(tmp_path):
    L = make(tmp_path)
    L.record("dev", "a", {"name": "x"}, {}, {"excess_sharpe_monthly": 0.1})
    L.record("dev", "a", {"name": "x"}, {}, {"excess_sharpe_monthly": 0.2})   # re-run of the same config
    L.record("validate", "b", {"name": "y"}, {}, {"excess_sharpe_monthly": 0.05})
    assert L.n_trials() == 2 and sorted(L.trial_sharpes()) == [0.05, 0.2]
    assert L.entries()[0]["trial_no"] == 1


def test_holdout_is_one_look_only(tmp_path):
    L = make(tmp_path)
    L.record("holdout", "final", {}, {}, {})
    with pytest.raises(lg.SealedHoldoutError):
        L.record("holdout", "tweaked", {}, {}, {})
    assert len(L.entries()) == 1            # the refused look left no entry
    L.record("holdout", "tweaked", {}, {}, {}, accept_contaminated=True)
    assert L.holdout_state()["contaminated"] is True


def test_holdout_reads_do_not_count_as_trials(tmp_path):
    L = make(tmp_path)
    L.record("holdout", "final", {}, {}, {})
    assert L.n_trials() == 0 and L.n_trials(include_holdout=True) == 1


def test_other_splits_are_never_blocked(tmp_path):
    L = make(tmp_path)
    for _ in range(3):
        L.record("dev", "a", {}, {}, {})
    L.check("forward", "z")
