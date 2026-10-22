import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import prices as px  # noqa: E402
import thesis_scorer as ts  # noqa: E402
from scorer import Cohort  # noqa: E402

DAY = 86400
T0 = 1_420_000_000


def grade(post_id="p1", cik="1", matured=T0, author="a", votes=10, stance=1.0, q=80, horizon="months",
          is_thesis=True, confidence=0.9, **kw):
    g = {"post_id": post_id, "cik": cik, "ticker": "T" + cik, "matured_utc": matured, "author": author,
         "votes": votes, "is_thesis": is_thesis, "stance": stance, "horizon": horizon,
         "argument_quality": q, "evidence_quality": q, "specificity": q, "tone_stability": q,
         "falsifiability": q, "balance": q, "hype_risk": 0, "confidence": confidence,
         "independent_support": 50, "independent_opposition": 50, "strongest_objection_severity": 0,
         "n_independent_voices": 1, "model": "fake", "prompt_version": "v"}
    g.update(kw)
    return g


class View:
    def __init__(self, rows):
        self.rows = rows

    def posts(self, cik=None):
        return [r for r in self.rows if cik is None or r["cik"] == cik]


def cohort(rows, cutoff, universe):
    return Cohort("m", cutoff, View(rows), universe)


def test_quality_weights_and_hype_penalty():
    p = ts.Params()
    # discussion term: (support 50 + opposition 50 + objection 0) / 3 = 33.3, weight .30
    disc = 0.30 * (50 + 50 + 0) / 3
    assert ts.thread_quality(grade(q=100), p) == pytest.approx(70 + disc)
    assert ts.thread_quality(grade(q=100, hype_risk=100), p) == pytest.approx(70 + disc - 10)
    assert ts.thread_quality(grade(q=0), p) == pytest.approx(disc)
    assert ts.thread_quality(grade(is_thesis=False, q=100), p) == pytest.approx(70 + disc)   # no gate by default
    assert ts.thread_quality(grade(is_thesis=False), ts.Params(thesis_gate="hard")) == 0
    soft = ts.Params(thesis_gate="soft")
    assert ts.thread_quality(grade(is_thesis=False, q=100), soft) == pytest.approx((70 + disc) * 0.5)
    assert ts.thread_quality(grade(is_thesis=False, q=100, is_thesis_p=0.2), soft) == pytest.approx((70 + disc) * 0.2)
    assert ts.thread_quality(grade(is_thesis=True, q=100, is_thesis_p=0.2), soft) == pytest.approx(70 + disc)
    # specificity is out of Q unless asked for; the first formula is still reachable
    assert ts.thread_quality(grade(q=100, specificity=0), p) == ts.thread_quality(grade(q=100), p)
    assert ts.thread_quality(grade(q=100, specificity=0), ts.Params(use_specificity=True)) < ts.thread_quality(grade(q=100), p)
    v1 = ts.Params(weights=dict(ts.WEIGHTS_V1), corroboration="net", hype_penalty=20, thesis_gate="hard",
                   use_specificity=True)
    assert ts.thread_quality(grade(q=100), v1) == pytest.approx(100 * (1 - 0.18) + 0.18 * 50)
    assert ts.thread_quality(grade(is_thesis=False), v1) == 0
    # more substantive discussion, either way, raises Q; the raw nested shape works too
    raw = {**grade(q=60), "comments": {"independent_support": 90, "independent_opposition": 60,
                                        "strongest_objection_severity": 30}}      # discussion 60 > 33
    assert ts.thread_quality(raw, p) > ts.thread_quality(grade(q=60), p)
    against = {**grade(q=60), "comments": {"independent_support": 10, "independent_opposition": 90,
                                            "strongest_objection_severity": 90}}   # 63 > 60, though bearish
    assert ts.thread_quality(against, p) > ts.thread_quality(raw, p)
    assert ts.thread_quality(raw, ts.Params(use_comments=False)) == pytest.approx(0.7 * 60)
    assert ts.contribution(grade(is_thesis=False, stance=1), ts.Params(thesis_gate="hard")) == 0
    assert ts.contribution(grade(is_thesis=False, stance=1), p) > 0


def test_contribution_sign_votes_and_decay():
    p = ts.Params(gamma=0.5)
    bull, bear = ts.contribution(grade(stance=1), p), ts.contribution(grade(stance=-1), p)
    assert bull > 0 and bear == -bull
    assert ts.contribution(grade(votes=1000), p) > ts.contribution(grade(votes=0), p)
    assert ts.contribution(grade(votes=1000), ts.Params(gamma=0)) == ts.contribution(grade(votes=0), ts.Params(gamma=0))
    assert ts.decay(90, "months", p) == pytest.approx(0.5) and ts.decay(0, "days", p) == 1
    assert ts.decay(14, "days", p) == pytest.approx(0.5) and ts.decay(60, None, p) == pytest.approx(0.5)


def test_score_sums_visible_threads_with_decay_and_gates():
    g1 = grade("p1", matured=T0 - 90 * DAY, horizon="months")      # half decayed
    g2 = grade("p2", matured=T0 - 1 * DAY, horizon="years", stance=-1, q=40)
    g3 = grade("p3", cik="2", matured=T0 - DAY, evidence_quality=10)
    s = ts.ThesisScorer([g1, g2, g3], ts.Params())
    rows = [{"post_id": "p1", "cik": "1"}, {"post_id": "p2", "cik": "1"}, {"post_id": "p2", "cik": "1"},
            {"post_id": "p3", "cik": "2"}, {"post_id": "p9", "cik": "1"}]   # p9 ungraded, dup p2
    out = s.score(cohort(rows, T0, {"1": {"code": "A"}, "2": {"code": "B"}}))
    expect = (ts.contribution(g1, s.p) * ts.decay(90, "months", s.p)
              + ts.contribution(g2, s.p) * ts.decay(1, "years", s.p))
    assert out["1"]["score"] == pytest.approx(expect) and out["1"]["n_threads"] == 2
    assert out["2"]["n_threads"] == 1
    # universe filter and the evidence gate
    assert "2" not in s.score(cohort(rows, T0, {"1": {"code": "A"}}))
    gated = ts.ThesisScorer([g1, g2, g3], ts.Params(min_evidence=50)).score(
        cohort(rows, T0, {"1": {}, "2": {}}))
    assert "1" in gated and "2" not in gated
    assert s.params()["gamma"] == 0.5 and s.params()["variant"] == "identified"


def test_author_prior_is_causal_and_shrunk():
    p = ts.Params(use_author=True, author_k=1.0)
    rows = [grade("a1", matured=T0, author="al", q=100), grade("a2", matured=T0 + DAY, author="al", q=100),
            grade("b1", matured=T0 + 2 * DAY, author="bo", q=0), grade("a3", matured=T0 + 3 * DAY, author="al", q=0)]
    pr = ts.author_priors(rows, p)
    assert pr[("a1", "1")] == pytest.approx(0.5)             # nothing known yet: population default
    first_q = ts.thread_quality(rows[0], p)
    assert pr[("a2", "1")] == pytest.approx((0.5 * first_q + 0.5 * first_q) / 100)
    # bo has no history: the running population mean (two good threads so far)
    assert pr[("b1", "1")] == pytest.approx(first_q / 100)
    # al after three threads: own mean (first_q) shrunk toward the population mean, which now
    # includes bo's bad thread, so it sits between the two
    pop = (2 * first_q + ts.thread_quality(rows[2], p)) / 3
    assert pop / 100 < pr[("a3", "1")] < first_q / 100
    # a later great thread never changes an earlier prior
    later = rows + [grade("b2", matured=T0 + 10 * DAY, author="bo", q=100)]
    assert ts.author_priors(later, p)[("b1", "1")] == pr[("b1", "1")]


def flat_series(code, closes, start_day):
    n = len(closes)
    days = np.arange(start_day, start_day + n)
    c = np.asarray(closes, dtype=float)
    return px.Series(code, days, c, c, c, c, c, np.ones(n))


class Store:
    def __init__(self, s):
        self.s = s

    def get(self, code):
        return self.s if code == self.s.code else None


def test_price_discount_uses_only_prices_at_or_before_the_cutoff():
    d0 = T0 // DAY
    store = Store(flat_series("A", [10, 10, 20, 40], d0))
    assert ts.price_ratio(store, "A", d0, d0 + 2) == pytest.approx(2.0)
    assert ts.price_ratio(store, "A", d0, d0 + 10) == pytest.approx(4.0)    # last bar before cutoff
    assert ts.price_ratio(store, "Z", d0, d0 + 2) is None
    with pytest.raises(ValueError):
        ts.price_ratio(store, "A", d0 + 2, d0)
    p = ts.Params(beta=1.0)
    assert ts.discount(2.0, p) == pytest.approx(0.5) and ts.discount(0.5, p) == 1.0   # capped
    assert ts.discount(2.0, ts.Params(beta=0.0)) == 1.0
    g = grade("p1", matured=T0, horizon="years")
    s = ts.ThesisScorer([g], p, store=store)
    full = ts.ThesisScorer([g], ts.Params(), store=store).thread_value(g, T0 + 2 * DAY, "A")
    assert s.thread_value(g, T0 + 2 * DAY, "A") == pytest.approx(full * 0.5)


def test_timeline_is_chronological_and_decays():
    p = ts.Params(gamma=0)
    rows = [grade("p2", matured=T0 + 90 * DAY, horizon="months"), grade("p1", matured=T0, horizon="months")]
    ev = ts.timeline(rows, p)
    assert [e["post_id"] for e in ev] == ["p1", "p2"]
    x = ts.contribution(rows[1], p)
    assert ev[0]["score"] == pytest.approx(x) and ev[1]["score"] == pytest.approx(x + x * 0.5)


def test_params_from_json_rejects_unknown_keys():
    p = ts.Params.from_json('{"gamma": 0, "half_life": {"days": 7}}')
    assert p.gamma == 0 and p.half_life["days"] == 7 and p.half_life["years"] == 180
    with pytest.raises(SystemExit):
        ts.Params.from_json('{"nope": 1}')


def test_decile_rates_rank_within_month():
    n = 40
    cand = {"month": np.array(["a"] * 20 + ["b"] * 20), "score": np.arange(n, dtype=float),
            "status": np.array(["complete"] * n), "max_mult": np.where(np.arange(n) % 20 >= 18, 6.0, 1.0),
            "min_mult": np.ones(n), "perm_loss": np.zeros(n, dtype=bool), "cagr": np.linspace(0, 1, n),
            "excess": np.zeros(n)}
    r = ts.decile_rates(cand)
    assert r[10]["n"] == 4 and r[10]["p5"] == 1.0 and r[1]["p5"] == 0.0
    assert "decile" in ts.calibrate(r)


def test_exclude_late_drops_threads_with_later_content():
    clean, late, edited = grade("p1", cik="1"), grade("p2", cik="2"), grade("p3", cik="3")
    late["late_shown"], edited["edited_late"] = 2, True
    assert not ts.is_late(clean) and ts.is_late(late) and ts.is_late(edited)
    keep = ts.ThesisScorer([clean, late, edited], ts.Params())
    drop = ts.ThesisScorer([clean, late, edited], ts.Params.from_json('{"exclude_late": true}'))
    assert len(keep.grades) == 3 and set(drop.grades) == {("p1", "1")}
    assert drop.params()["exclude_late"] is True and drop.params()["pit"]["comments"] == "all"
    assert {r["post_id"] for r in ts.timeline([clean, late], ts.Params(exclude_late=True))} == {"p1"}
