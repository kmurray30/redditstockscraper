import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import backtest as bt  # noqa: E402
import ledger as lg  # noqa: E402
import prices as px  # noqa: E402
import universe as un  # noqa: E402

MONTHS = [f"2012-{m:02d}-01" for m in range(1, 13)] + [f"2013-{m:02d}-01" for m in range(1, 13)]
N = 60


def winner(month, i):
    return (hash((month, i)) % 10) == 0 if False else (i * 7 + int(month[5:7]) * 3) % 10 == 0


def write_world(tmp_path, *, status="complete"):
    uni, labels = [], []
    for month in MONTHS:
        for i in range(N):
            cik = f"{i:04d}"
            uni.append({"month": month, "cik": cik, "ticker": f"T{i}", "code": f"T{i}",
                        "sec_type": "CS", "price": 10.0, "adv60": 1e6 + i, "mom": 0.0,
                        "eligible": True, "size_q": 1 + i % 5})
            w = winner(month, i)
            for years in (1, 3, 5):
                labels.append({"month": month, "cik": cik, "years": years, "status": status,
                               "ret": 5.0 if w else 0.0, "cagr": 0.5 if w else 0.0,
                               "excess": 4.0 if w else -0.1, "max_mult": 6.0 if w else 1.2,
                               "min_mult": 0.9, "max_dd": -0.1, "d2": 100 if w else None,
                               "d5": 300 if w else None, "d10": None, "perm_loss": False,
                               "entry_day": 15000, "end_day": 15000 + 365 * years})
    f = tmp_path / "features"
    f.mkdir()
    pq.write_table(pa.Table.from_pylist(uni, schema=un.UNIVERSE_SCHEMA), f / "universe.parquet")
    pq.write_table(pa.Table.from_pylist(labels, schema=un.LABEL_SCHEMA), f / "labels.parquet")
    return f


class FakeCorpus:
    def at(self, cutoff, window):
        return None


class Fn:
    """A scorer from a function (month, cik) -> score; counts how often it is called."""
    version, lookback_days, maturation_hours = "1", 90, 24

    def __init__(self, name, fn):
        self.name, self.fn, self.calls = name, fn, 0

    def params(self):
        return {}

    def score(self, cohort):
        self.calls += 1
        return {c: {"score": self.fn(cohort.month, c)} for c in cohort.universe}


ORACLE = lambda m, c: 1.0 if winner(m, int(c)) else 0.0  # noqa: E731


def rand(m, c):
    return bt._tiebreak(7, m, c)


def test_select_top_takes_the_best_fraction_per_month():
    months = ["m1"] * 20 + ["m2"] * 20
    ciks = [f"c{i}" for i in range(20)] * 2
    scores = list(range(20)) * 2
    mask = bt.select_top(months, ciks, scores, 0.1)
    assert mask.sum() == 4 and set(np.array(scores)[mask]) == {18, 19}


def test_select_top_skips_thin_months_and_ties_do_not_depend_on_input_order():
    assert bt.select_top(["m"] * 5, list("abcde"), [1] * 5, 0.5, min_candidates=10).sum() == 0
    ciks = [f"c{i}" for i in range(30)]
    scores = [1.0] * 30                                   # everything tied
    a = bt.select_top(["m"] * 30, ciks, scores, 0.2, seed=3)
    perm = list(reversed(range(30)))
    b = bt.select_top(["m"] * 30, [ciks[i] for i in perm], [scores[i] for i in perm], 0.2, seed=3)
    assert {ciks[i] for i in np.nonzero(a)[0]} == {ciks[perm[i]] for i in np.nonzero(b)[0]}


def run_scorer(tmp_path, scorer, split_months=None, **kw):
    f = write_world(tmp_path)
    ev = bt.Evaluator(f)
    # use the whole synthetic range as the "dev" split
    lg.SPLITS["dev"] = ("2012-01-01", "2013-12-01")
    led = lg.Ledger(tmp_path / "ledger")
    return bt.run(scorer, ev, FakeCorpus(), "dev", ledger=led, cache_dir=None, n_boot=100,
                  n_perm=100, horizons=(1,), log=lambda m: None, **kw), led


@pytest.fixture(autouse=True)
def restore_splits():
    saved = dict(lg.SPLITS)
    yield
    lg.SPLITS.clear()
    lg.SPLITS.update(saved)


def test_an_oracle_shows_big_lift_and_a_significant_permutation_p(tmp_path):
    rep, _ = run_scorer(tmp_path, Fn("oracle", ORACLE))
    h = rep["horizons"][1]
    assert h["lift"]["p5"] > 5 and h["perm_p5"] < 0.05
    assert h["top"]["p5"] == pytest.approx(1.0) and h["universe"]["p5"] == pytest.approx(0.1, abs=0.03)
    lo, hi = h["lift_ci"]["p5"]
    assert lo > 3


def test_a_random_scorer_has_no_lift_and_no_significance(tmp_path):
    rep, _ = run_scorer(tmp_path, Fn("random", rand))
    h = rep["horizons"][1]
    assert 0.3 < h["lift"]["p5"] < 2.5
    lo, hi = h["lift_ci"]["p5"]
    assert lo < 1.0 < hi and h["perm_p5"] > 0.05


def test_censored_rows_are_excluded_from_rates_not_counted_as_failures():
    a = {"status": np.array(["complete", "censored", "censored"]),
         "max_mult": np.array([6.0, 1.0, 1.0]), "min_mult": np.ones(3),
         "perm_loss": np.zeros(3, bool), "cagr": np.zeros(3), "excess": np.zeros(3)}
    r = bt.event_rates(a)
    assert r["n"] == 1 and r["p5"] == 1.0


def test_km_uses_censored_follow_up():
    # one 5x at day 500; two holdings were only observed for 200 days (censored)
    a = {"status": np.array(["complete", "complete", "censored", "censored"]),
         "d5": np.array([500.0, np.nan, np.nan, np.nan]),
         "entry_day": np.zeros(4), "end_day": np.array([1095, 1095, 200, 200.0])}
    km = bt.km_touch(a, 5, 3)
    assert km == pytest.approx(0.5)    # 2 holdings still at risk at day 500; naive 1/4 would be 0.25


def test_scores_are_cached_per_scorer_configuration(tmp_path):
    f = write_world(tmp_path)
    ev = bt.Evaluator(f)
    sc = Fn("cached", ORACLE)
    bt.build_scores(sc, MONTHS[:3], FakeCorpus(), ev.con, tmp_path / "cache", log=lambda m: None)
    first = sc.calls
    bt.build_scores(sc, MONTHS[:3], FakeCorpus(), ev.con, tmp_path / "cache", log=lambda m: None)
    assert sc.calls == first
    bt.build_scores(sc, MONTHS[:5], FakeCorpus(), ev.con, tmp_path / "cache", log=lambda m: None)
    assert sc.calls == first + 2        # only the two new months


def test_second_look_at_the_holdout_is_refused_before_any_scoring(tmp_path):
    f = write_world(tmp_path)
    lg.SPLITS["holdout"] = ("2012-01-01", "2013-12-01")
    ev, led = bt.Evaluator(f), lg.Ledger(tmp_path / "ledger")
    first = Fn("one", ORACLE)
    bt.run(first, ev, FakeCorpus(), "holdout", ledger=led, cache_dir=None, n_boot=50, n_perm=50,
           horizons=(1,), log=lambda m: None)
    second = Fn("two", rand)
    with pytest.raises(lg.SealedHoldoutError):
        bt.run(second, ev, FakeCorpus(), "holdout", ledger=led, cache_dir=None, horizons=(1,),
               log=lambda m: None)
    assert second.calls == 0


def test_run_records_the_trial(tmp_path):
    rep, led = run_scorer(tmp_path, Fn("oracle", ORACLE))
    e = led.entries()[0]
    assert e["config_id"] == rep["config_id"] and e["split"] == "dev"
    assert e["summary"]["lift_p5_1y"] > 5


# ------------------------------------------------------------ portfolio

def flat_series(code, closes, start="2012-01-02"):
    n = len(closes)
    c = np.array(closes, float)
    return px.Series(code, px.to_day(start) + np.arange(n), c, c, c, c, c, np.full(n, 1e6))


class Store:
    def __init__(self, series):
        self.s = series

    def get(self, code):
        return self.s.get(code)


def test_portfolio_equal_weights_lots_and_holds_delisted_names_flat():
    up = flat_series("UP", np.linspace(100, 200, 400))
    dead = flat_series("DEAD", [100.0] * 40 + [50.0] * 3)      # ends after ~40 days at -50%
    spy = flat_series("SPY", np.linspace(100, 110, 400))
    st = Store({"UP": up, "DEAD": dead, "SPY": spy})
    rep = bt.simulate_portfolio([("2012-01-01", "1", "UP"), ("2012-01-01", "2", "DEAD")], st, 6,
                                slippage_bps=0)
    assert rep["positions"] == 2 and rep["months"] >= 6
    assert rep["name_hit_rate"] == pytest.approx(0.5)
    assert rep["max_dd"] < 0 and rep["top5_share_of_gains"] == pytest.approx(1.0)


def test_month_end_values_use_the_last_bar_of_each_month():
    s = flat_series("A", np.arange(1, 61), start="2012-01-25")   # Jan 25 .. into March
    mi, mv = bt.month_end_values(s)
    assert len(mi) == 3 and mv[0] == 7        # Jan 25..31 is 7 bars


def test_bad_data_rows_are_excluded_not_counted_as_wins_or_losses():
    a = {"status": np.array(["complete", "bad_data", "complete"]),
         "max_mult": np.array([1.0, 200000.0, 6.0]), "min_mult": np.ones(3),
         "perm_loss": np.zeros(3, bool), "cagr": np.zeros(3), "excess": np.zeros(3)}
    r = bt.event_rates(a)
    assert r["n"] == 2 and r["p5"] == 0.5 and r["bad_data"] == 1


def test_km_ignores_bad_data():
    a = {"status": np.array(["complete", "bad_data", "complete"]),
         "d5": np.array([np.nan, 10.0, np.nan]), "entry_day": np.zeros(3),
         "end_day": np.full(3, 1095.0)}
    assert bt.km_touch(a, 5, 3) == 0.0


def test_load_scorer_from_a_file_and_rejects_non_scorers(tmp_path):
    good = tmp_path / "my_scorer.py"
    good.write_text("""
class S:
    name, version, lookback_days, maturation_hours = 'mine', '1', 30, 24
    def params(self): return {}
    def score(self, cohort): return {}
def make(): return S()
def bad(): return object()
""")
    sc = bt.load_scorer(f"{good}:make")
    assert sc.name == "mine"
    with pytest.raises(SystemExit):
        bt.load_scorer(f"{good}:bad")
    with pytest.raises(SystemExit):
        bt.load_scorer("nocolon")


def test_dedupe_counts_a_sustained_story_once():
    months = ["m1", "m2", "m3", "m4", "m5"] * 2
    ciks = ["A"] * 5 + ["B"] * 5
    mask = np.array([True, True, True, False, True] + [True] + [False] * 4)
    out = bt.dedupe_selection(months, ciks, mask, 1)
    # A selected m1,m2,m3,(not m4),m5: m2/m3 are within 1 month of the previous kept pick or
    # selection... only m1 kept, then m3 (2 months after m1), then m5 (2 after m3)
    assert list(out[:5]) == [True, False, True, False, True] and out[5]
    assert bt.dedupe_selection(months, ciks, mask, 0).tolist() == mask.tolist()


def test_permutation_strata_include_sector_when_available():
    cand = {"month": np.array(["m"] * 40), "size_q": np.ones(40, int),
            "status": np.array(["complete"] * 40), "score": np.arange(40, dtype=float),
            "max_mult": np.where(np.arange(40) >= 36, 6.0, 1.0),
            "division": np.array(["D"] * 20 + ["I"] * 20)}
    ev = bt.Evaluator.__new__(bt.Evaluator)
    p_sector = ev._permute(cand, 0.1, 100, 0, 10)
    del cand["division"]
    p_plain = ev._permute(cand, 0.1, 100, 0, 10)
    assert 0 < p_sector <= 1 and 0 < p_plain <= 1
