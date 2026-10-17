import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import stats  # noqa: E402


def test_km_matches_the_plain_fraction_without_censoring():
    d = [1, 2, 3, 4, 100, 100, 100, 100, 100, 100]
    e = [1, 1, 1, 1, 0, 0, 0, 0, 0, 0]
    assert stats.km_event_prob(d, e, 10) == pytest.approx(0.4)


def test_km_does_not_treat_censored_as_failures():
    # 10 holdings, 2 events; 6 censored early (only observed 1 unit): naive 2/10 = 0.2
    d = [5, 5] + [1] * 6 + [100, 100]
    e = [1, 1] + [0] * 6 + [0, 0]
    p = stats.km_event_prob(d, e, 10)
    assert p > 0.2 and p == pytest.approx(1 - (1 - 2 / 4))  # 4 still at risk at t=5


def test_km_only_counts_events_up_to_t():
    assert stats.km_event_prob([1, 20], [1, 1], 10) == pytest.approx(0.5)


def test_km_empty_is_nan():
    assert np.isnan(stats.km_event_prob([], [], 5))


def test_block_bootstrap_resamples_blocks_and_ci_covers_truth():
    rng = np.random.default_rng(1)
    blocks = {f"b{i}": rng.normal(1.0, 1.0, 20) for i in range(30)}
    draws = stats.block_bootstrap(blocks, lambda bs: float(np.concatenate(bs).mean()), n=300)
    lo, hi = stats.percentile_ci(draws)
    assert lo < 1.0 < hi and hi - lo < 0.5


def test_permutation_detects_real_signal_and_not_noise():
    rng = np.random.default_rng(2)

    def stat(strata):  # mean outcome of the top-scored element in each stratum
        return float(np.mean([o[np.argmax(s)] for s, o in strata]))

    informative = []
    for _ in range(40):
        s = rng.normal(size=10)
        informative.append((s, s + rng.normal(scale=0.5, size=10)))
    p_real = stats.permutation_p(informative, stat, stat(informative), n=200)
    noise = [(rng.normal(size=10), rng.normal(size=10)) for _ in range(40)]
    p_noise = stats.permutation_p(noise, stat, stat(noise), n=200)
    assert p_real < 0.02 and p_noise > 0.05


def test_deflated_sharpe_shrinks_with_more_trials():
    one = stats.deflated_sharpe(0.3, 120, 0.0, 3.0, 1, 0.01)
    many = stats.deflated_sharpe(0.3, 120, 0.0, 3.0, 200, 0.01)
    assert one > many and 0 <= many <= 1
    assert stats.deflated_sharpe(0.0, 120, 0.0, 3.0, 50, 0.01) < 0.05


def test_expected_max_sharpe_grows_with_trials():
    assert stats.expected_max_sharpe(1, 0.01) == 0.0
    assert stats.expected_max_sharpe(100, 0.01) > stats.expected_max_sharpe(10, 0.01) > 0


def test_sharpe_moments_degenerate_series():
    assert np.isnan(stats.sharpe_moments([1.0, 1.0, 1.0])[0])
    sr, skew, kurt, n = stats.sharpe_moments(np.random.default_rng(0).normal(0.01, 0.05, 500))
    assert n == 500 and abs(skew) < 0.5 and 2 < kurt < 4
