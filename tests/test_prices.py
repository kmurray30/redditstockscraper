import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prices as px  # noqa: E402

START = px.to_day("2020-01-01")


def make(code, closes, *, start=START, opens=None, volume=1_000_000):
    n = len(closes)
    days = start + np.arange(n)
    closes = np.array(closes, dtype=float)
    opens = closes if opens is None else np.array(opens, dtype=float)
    return px.Series(code, days, opens, closes, closes, closes, closes, np.full(n, float(volume)))


def test_to_day_roundtrip():
    assert px.to_iso(px.to_day("2012-01-03")) == "2012-01-03"


def test_entry_index_is_first_bar_on_or_after():
    s = make("A", [1, 2, 3])
    assert s.entry_index(START + 1) == 1
    assert s.entry_index(START - 3) == 0
    assert s.entry_index(START + 3 + px.MAX_ENTRY_GAP + 1) is None  # nothing trading then


def test_entry_is_after_the_cutoff_day_and_uses_that_bars_open():
    s = make("A", [10, 10, 10, 10], opens=[9, 11, 12, 13])
    i = s.entry_index(START + 1)          # cutoff day is START; first bar strictly after it
    o = px.outcome(s, i, 0.005, data_end=START + 3, slippage_bps=0)
    assert o.entry_day == START + 1 and o.ret == pytest.approx(10 / 11 - 1)


def test_complete_horizon_ret_and_touches():
    closes = [100.0] * 5 + list(np.linspace(100, 600, 400))
    s = make("A", closes)
    o = px.outcome(s, 0, 1.0, data_end=START + 1000, slippage_bps=0)
    assert o.status == "complete"
    assert o.days_to[2] is not None and o.days_to[5] is not None and o.days_to[10] is None
    assert o.max_mult >= 5 and not o.perm_loss


def test_censored_when_data_ends_before_the_horizon_and_never_a_loss():
    s = make("A", [100.0] * 30 + [10.0])  # collapses, but the horizon is not over
    o = px.outcome(s, 0, 5.0, data_end=s.last_day, slippage_bps=0)
    assert o.status == "censored" and not o.perm_loss


def test_delisted_series_ending_early_is_a_permanent_loss_when_worthless():
    s = make("BAD", [100.0] * 60 + [5.0] * 5)
    o = px.outcome(s, 0, 1.0, data_end=START + 2000, slippage_bps=0)
    assert o.status == "delisted" and o.perm_loss and o.ret == pytest.approx(-0.95)


def test_acquired_company_is_delisted_but_not_a_loss():
    s = make("ACQ", [100.0] * 60 + [140.0] * 5)
    o = px.outcome(s, 0, 1.0, data_end=START + 2000, slippage_bps=0)
    assert o.status == "delisted" and not o.perm_loss and o.ret == pytest.approx(0.4)


def test_max_drawdown_is_peak_to_trough_after_entry():
    s = make("A", [100, 200, 100, 50, 80])
    o = px.outcome(s, 0, 0.01, data_end=s.last_day, slippage_bps=0)
    assert o.max_dd == pytest.approx(-0.75)


def test_slippage_reduces_the_return():
    s = make("A", [100.0] * 400)
    o = px.outcome(s, 0, 1.0, data_end=START + 1000, slippage_bps=50)
    assert o.ret < 0


def test_excess_return_uses_the_benchmark_over_the_same_window():
    s = make("A", np.linspace(100, 200, 400))
    b = make("SPY", np.linspace(100, 150, 400))
    o = px.outcome(s, 0, 1.0, data_end=START + 1000, bench=b, slippage_bps=0)
    assert o.excess is not None and o.excess > 0


def test_trailing_dollar_volume_and_momentum():
    s = make("A", list(range(1, 400)), volume=1000)
    assert px.trailing_dollar_volume(s, 100, 60) == pytest.approx(np.median(
        np.arange(42, 102) * 1000.0))
    assert px.trailing_dollar_volume(s, 5, 60) is None  # too little history
    assert px.momentum(s, 300) == pytest.approx(s.close[279] / s.close[48] - 1)
    assert px.momentum(s, 100) is None


def test_series_from_rows_adjusts_open_high_low_and_drops_bad_bars():
    rows = {"date": ["2020-01-02", "2020-01-03", "2020-01-04"],
            "open": [10.0, 10.0, 10.0], "high": [12.0, 12.0, 12.0], "low": [9.0, 9.0, 9.0],
            "close": [10.0, 10.0, 0.0], "adjusted_close": [5.0, 5.0, 0.0],
            "volume": [1.0, 1.0, 1.0]}
    s = px.series_from_rows("X", rows)
    assert len(s) == 2 and s.open[0] == pytest.approx(5.0) and s.high[0] == pytest.approx(6.0)
    assert s.raw_close[0] == 10.0


# ------------------------------------------------------------ cleaning (real failures seen in the data)

def test_isolated_bad_tick_is_dropped():
    s = make("A", [10, 10, 298098.0, 10, 10, 10])        # SBOT-style print
    clean, n = px.drop_spikes(s)
    assert n == 1 and clean.close.max() == 10 and len(clean) == 5


def test_zero_then_spike_pairs_do_not_survive():
    s = make("A", [34, 34, 34, 340000.0, 34, 34])
    assert px.drop_spikes(s)[0].close.max() == 34


def test_a_real_persistent_move_is_not_a_spike():
    s = make("A", [10, 10, 10, 100, 100, 100])           # a 10x that stays: not a bad tick
    clean, n = px.drop_spikes(s)
    assert n == 0 and len(clean) == 6


def test_unadjusted_reverse_split_is_repaired():
    # 1-for-10 reverse split on day 5: vendor left the history unadjusted, price jumps 10x
    s = make("A", [1.0] * 5 + [10.0] * 5)
    day = int(s.days[5])
    fixed, n = px.fix_unadjusted_splits(s, [(day, 0.1)])
    assert n == 1 and np.allclose(fixed.close, 10.0)


def test_an_already_adjusted_split_is_left_alone():
    s = make("A", [10.0] * 10)
    fixed, n = px.fix_unadjusted_splits(s, [(int(s.days[5]), 0.1)])
    assert n == 0 and np.allclose(fixed.close, 10.0)


def test_a_jump_unrelated_to_any_split_is_not_touched():
    s = make("A", [1.0] * 5 + [10.0] * 5)
    assert px.fix_unadjusted_splits(s, [(int(s.days[5]), 0.5)])[1] == 0   # ratio doesn't match


def test_unexplained_huge_move_marks_the_window_bad_not_a_10x():
    closes = [10.0] * 40 + [100.0] * 40            # a 10x in one day that stays, no split to blame
    s = px.clean_series(make("A", closes))
    assert len(s.bad_days) == 1
    o = px.outcome(s, 0, 0.2, data_end=START + 1000, slippage_bps=0)
    assert o.status == "bad_data" and not o.perm_loss
    after = px.outcome(s, 50, 0.1, data_end=START + 1000, slippage_bps=0)
    assert after.status != "bad_data"               # windows entirely after the jump are fine


def test_clean_series_end_to_end():
    closes = [10.0] * 30 + [298098.0] + [10.0] * 30
    s = px.clean_series(make("A", closes))
    assert s.close.max() == 10.0 and len(s.bad_days) == 0


def series_with_ratio(closes, raws, start="2015-01-02"):
    n = len(closes)
    c, r = np.array(closes, float), np.array(raws, float)
    return px.Series("A", px.to_day(start) + np.arange(n), c, c, c, c, r, np.full(n, 1e6))


def test_adjustment_consistent_with_splits_and_dividends_passes():
    # 2-for-1 split on day 5 (ratio 2): earlier adjusted prices are half the raw ones;
    # a small dividend factor (0.95) applies throughout
    raw = [100.0] * 5 + [50.0] * 5
    adj = [100 / 2 * 0.95] * 5 + [50 * 0.95] * 5
    s = series_with_ratio(adj, raw)
    assert len(px.inconsistent_adjustment_days(s, [(int(s.days[5]), 2.0)])) == 0


def test_corrupt_vendor_adjustment_is_flagged():
    # EE_old: raw ~$1.05 but adjusted 0.0017, and no splits to explain a 600x factor
    s = series_with_ratio([0.0017] * 10, [1.05] * 10)
    assert len(px.inconsistent_adjustment_days(s, [])) == 10


def test_adjusted_above_raw_is_flagged():
    s = series_with_ratio([8.0] * 4, [1.0] * 4)           # SMLP-style: factor 8 with no split
    assert len(px.inconsistent_adjustment_days(s, [])) == 4


def test_no_split_data_means_no_consistency_check():
    s = series_with_ratio([0.0017] * 4, [1.05] * 4)
    assert len(px.inconsistent_adjustment_days(s, None)) == 0


def test_a_repaired_unadjusted_split_is_not_then_flagged():
    # price quoted 1.0 -> 10.0 after a 1-for-10 reverse split; vendor left adj == raw (unadjusted)
    raw = [1.0] * 5 + [10.0] * 5
    s = series_with_ratio(raw, raw)
    cleaned = px.clean_series(s, [(int(s.days[5]), 0.1)])
    assert len(cleaned.bad_days) == 0 and np.allclose(cleaned.close, 10.0)


def test_corrupt_series_makes_every_overlapping_outcome_bad_data():
    s = px.clean_series(series_with_ratio([0.0017] * 400, [1.05] * 400), [])
    o = px.outcome(s, 0, 1.0, data_end=int(s.days[-1]) + 400, slippage_bps=0)
    assert o.status == "bad_data"


def test_dollar_volume_has_no_knowledge_of_later_splits():
    # 25 bars before a 20:1 split and 25 after; the stock trades 1,000 shares at $2,000 before
    # (= 20,000 at $100 after): $2M a day throughout. EODHD's volume is split-adjusted (20,000
    # before), so the raw close times it read $40M.
    days = np.arange(50, dtype=np.int64)
    raw = np.r_[np.full(25, 2000.0), np.full(25, 100.0)]
    adj = np.full(50, 100.0)
    vol = np.full(50, 20000.0)
    s = px.Series("X", days, adj, adj, adj, adj, raw, vol)
    assert px.trailing_dollar_volume(s, 24, 20) == pytest.approx(40e6)          # no split data: the old reading
    s = px.clean_series(s, [(25, 20.0)])
    assert px.trailing_dollar_volume(s, 24, 20) == pytest.approx(2e6)
    assert px.trailing_dollar_volume(s, 49, 20) == pytest.approx(2e6)
    # a later 1:20 reverse split, volume adjusted: the old formula read 20 times too small
    raw = np.r_[np.full(25, 1.0), np.full(25, 20.0)]
    adj = np.full(50, 20.0)
    s = px.clean_series(px.Series("Y", days, adj, adj, adj, adj, raw, np.full(50, 5000.0)), [(25, 0.05)])
    assert px.trailing_dollar_volume(s, 24, 20) == pytest.approx(1e5)            # 100,000 shares at $1
    # the same split with the volume left raw (100,000 shares before, 5,000 after): nothing to undo
    vol = np.r_[np.full(25, 100000.0), np.full(25, 5000.0)]
    s = px.clean_series(px.Series("Z", days, adj, adj, adj, adj, raw, vol), [(25, 0.05)])
    assert not px.volume_adjusted_for(s, 25, 0.05)
    assert px.trailing_dollar_volume(s, 24, 20) == pytest.approx(1e5)
    assert px.trailing_dollar_volume(s, 49, 20) == pytest.approx(1e5)
    # a split after the series ended belongs to a later holder of the code: nothing to undo
    s = px.clean_series(px.Series("W", days, adj, adj, adj, adj, raw, vol), [(80, 0.001)])
    assert not px.volume_adjusted_for(s, 80, 0.001)
    assert px.trailing_dollar_volume(s, 24, 20) == pytest.approx(1e5)
