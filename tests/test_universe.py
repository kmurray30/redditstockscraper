import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prices as px  # noqa: E402
import security_master as sm  # noqa: E402
import universe as un  # noqa: E402


class FakeStore:
    def __init__(self, series, data_end):
        self._s, self.data_end = series, data_end

    def get(self, code):
        return self._s.get(code)


def series(code, start, closes, volume=1_000_000):
    n = len(closes)
    c = np.array(closes, dtype=float)
    return px.Series(code, px.to_day(start) + np.arange(n), c, c, c, c, c, np.full(n, float(volume)))


def snap(as_of, ticker, cik, typ="CS"):
    return {"as_of": as_of, "ticker": ticker, "cik": cik, "name": ticker, "type": typ, "active": True}


def test_month_starts():
    assert un.month_starts("2011-11-01", "2012-02-01") == [
        "2011-11-01", "2011-12-01", "2012-01-01", "2012-02-01"]


@pytest.fixture
def world():
    snaps = [snap("2020-01-04", "AAA", "1"), snap("2020-01-04", "BBB", "2"),
             snap("2021-01-04", "AAA", "1"), snap("2021-01-04", "ETF1", "3", "ETF"),
             snap("2021-01-04", "NEW", "4")]
    idx = {"AAA": ("2020-01-01", "2021-12-31"), "BBB": ("2020-01-01", "2020-03-15"),
           "NEW": ("2020-03-01", "2021-12-31"), "SPY": ("2020-01-01", "2021-12-31")}
    master = sm.SecurityMaster(snaps, idx)
    ser = {
        "AAA": series("AAA", "2020-01-01", np.linspace(10, 40, 700)),
        "BBB": series("BBB", "2020-01-01", [10.0] * 75),                 # ends mid-March
        "NEW": series("NEW", "2020-03-01", np.linspace(5, 6, 500)),     # IPO'd between snapshots
        "SPY": series("SPY", "2020-01-01", np.linspace(100, 110, 700)),
    }
    return master, FakeStore(ser, px.to_day("2021-12-31"))


def test_universe_membership_and_eligibility(world):
    master, store = world
    uni, labels = un.build(master, store, ["2020-02-01", "2020-04-01", "2020-06-01"], horizons=(1,),
                           slippage_bps=0, log=lambda m: None)
    rows = {(r["month"], r["cik"]): r for r in uni.to_pylist()}
    assert ("2020-02-01", "1") in rows and ("2020-02-01", "2") in rows
    assert ("2020-04-01", "2") not in rows            # BBB stopped trading in March: gone
    assert ("2020-06-01", "4") in rows                # NEW is in the later snapshot and traded
    assert not any(r["cik"] == "3" for r in uni.to_pylist())  # ETFs are not in the universe
    assert rows[("2020-02-01", "1")]["eligible"]


def test_delisted_company_label_is_delisted_not_dropped(world):
    master, store = world
    uni, labels = un.build(master, store, ["2020-02-01"], horizons=(1,), slippage_bps=0,
                           log=lambda m: None)
    got = {r["cik"]: r for r in labels.to_pylist()}
    assert got["2"]["status"] == "delisted"
    assert got["1"]["status"] in {"complete", "censored"}


def test_entry_is_after_the_cohort_date(world):
    master, store = world
    _, labels = un.build(master, store, ["2020-02-01"], horizons=(1,), log=lambda m: None)
    assert all(r["entry_day"] > px.to_day("2020-02-01") for r in labels.to_pylist())


def test_size_quintiles_within_month():
    rows = [{"month": "m", "eligible": True, "adv60": float(i), "size_q": 0} for i in range(10)]
    rows.append({"month": "m", "eligible": False, "adv60": 5.0, "size_q": 0})
    un.assign_size_quintiles(rows)
    assert [r["size_q"] for r in rows[:10]] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert rows[10]["size_q"] == 0


def test_illiquid_and_penny_stocks_are_ineligible(world):
    master, _ = world
    store = FakeStore({"AAA": series("AAA", "2020-01-01", [0.5] * 700),
                       "SPY": series("SPY", "2020-01-01", [100.0] * 700)}, px.to_day("2021-12-31"))
    uni, _ = un.build(master, store, ["2020-06-01"], horizons=(1,), log=lambda m: None)
    assert [r["eligible"] for r in uni.to_pylist() if r["cik"] == "1"] == [False]


def test_companies_without_a_usable_series_are_reported_not_silently_lost(world):
    master, store = world
    # DEAD was listed in 2020, gone in 2021, and its code was taken over by another company
    # in 2022 (so EODHD's DEAD series is the new holder's history); no DEADQ/_old exists
    snaps = [snap("2020-01-04", "AAA", "1"), snap("2020-01-04", "DEAD", "9"),
             snap("2021-01-04", "AAA", "1"),
             snap("2022-01-04", "AAA", "1"), snap("2022-01-04", "DEAD", "10")]
    idx = {"AAA": ("2020-01-01", "2021-12-31"), "DEAD": ("2022-01-01", "2023-12-31"),
           "SPY": ("2020-01-01", "2021-12-31")}
    master = sm.SecurityMaster(snaps, idx)
    dropped = []
    logs = []
    uni, _ = un.build(master, store, ["2020-02-01", "2020-03-01"], horizons=(1,),
                      log=logs.append, dropped=dropped)
    assert {r["cik"] for r in uni.to_pylist()} == {"1"}
    assert dropped == [{"cik": "9", "ticker": "DEAD", "reason": "code_reused_after_delisting",
                        "months": 2}]
    assert any("code_reused_after_delisting=1" in line for line in logs)


def test_drop_reason_buckets():
    assert un.drop_reason("unknown_cik") == "unknown_cik"
    assert un.drop_reason("no_valid_series (X reused by another company)") == "code_reused_after_delisting"
    assert un.drop_reason("no_valid_series (X is a different company (Foo))") == "code_name_mismatch"
    assert un.drop_reason("no_valid_series") == "no_price_series"
