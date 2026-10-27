import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "security_master", Path(__file__).parents[1] / "scripts" / "security_master.py"
)
sm = importlib.util.module_from_spec(spec)
sys.modules["security_master"] = sm  # dataclasses look the module up by name
spec.loader.exec_module(sm)

KEY = "sekrit-key-123"


def make_client(responses, **kw):
    calls, sleeps = [], []

    def opener(url):
        calls.append(url)
        status, payload = responses.pop(0)
        return status, json.dumps(payload).encode()

    return sm.Massive(KEY, opener=opener, sleep=sleeps.append, clock=lambda: 0.0, **kw), calls, sleeps


def rec(ticker, cik, **kw):
    return {"ticker": ticker, "name": ticker + " Inc", "cik": cik, "active": True,
            "market": "stocks", **kw}


def test_snapshot_follows_next_url_and_keeps_cik_per_date():
    pages = [
        (200, {"results": [rec("BBBY", "0000886158")], "next_url": f"{sm.API}/v3/x?cursor=2"}),
        (200, {"results": [rec("AAPL", "0000320193")]}),
    ]
    c, calls, _ = make_client(pages)
    t = sm.pull_snapshot(c, "2022-08-16", log=lambda m: None)
    assert [r["ticker"] for r in t.to_pylist()] == ["BBBY", "AAPL"]
    assert {r["as_of"] for r in t.to_pylist()} == {"2022-08-16"}
    assert "date=2022-08-16" in calls[0] and calls[1].startswith(f"{sm.API}/v3/x?cursor=2")
    assert all(f"apiKey={KEY}" in u for u in calls)  # key appended to every page


def test_calls_are_spaced_for_the_free_plan():
    ticks = iter([0.0, 1.0, 1.0, 13.0, 13.0])
    sleeps = []
    c = sm.Massive(KEY, opener=lambda u: (200, b"{}"), sleep=sleeps.append,
                   clock=lambda: next(ticks), min_interval=12.5)
    c.get("a"); c.get("b"); c.get("c")
    assert sleeps == [11.5, 0.5]  # waits only the part of the interval not already elapsed


def test_retries_429_then_succeeds():
    c, calls, sleeps = make_client([(429, {}), (200, {"results": []})])
    assert c.get("v3/x") == {"results": []} and len(calls) == 2 and sleeps


def test_errors_never_contain_the_key():
    c, _, _ = make_client([(403, {"message": "plan"})])
    with pytest.raises(sm.MassiveError) as e:
        c.get("v3/x")
    assert KEY not in str(e.value)


def test_grid_dates_skip_the_future():
    d = sm.grid_dates(2010, 2012, per_year=2)
    assert d == ["2010-01-04", "2010-07-04", "2011-01-04", "2011-07-04",
                 "2012-01-04", "2012-07-04"]
    assert sm.grid_dates(2999, 2999) == []


def test_snapshot_path_rejects_odd_input():
    with pytest.raises(ValueError):
        sm.snapshot_path(Path("x"), "../../etc")


# ---------------------------------------------------------------- SecurityMaster

def snap(as_of, ticker, cik, name=None):
    return {"as_of": as_of, "ticker": ticker, "cik": cik, "name": name or ticker + " Inc",
            "active": True}


OLD, NEW = "0000886158", "0001130713"  # old Bed Bath & Beyond, Overstock/Beyond/BBBY
SNAPS = [
    snap("2021-01-04", "BBBY", OLD), snap("2021-01-04", "OSTK", NEW),
    snap("2022-01-04", "BBBY", OLD), snap("2022-01-04", "OSTK", NEW),
    snap("2023-01-04", "BBBY", OLD), snap("2023-01-04", "OSTK", NEW),
    snap("2024-01-04", "BYON", NEW),                     # BBBY (old) gone, OSTK renamed
    snap("2025-01-04", "BYON", NEW),
    snap("2026-01-04", "BBBY", NEW),                     # the code is reused by Overstock
    snap("2021-01-04", "FB", "0001326801"), snap("2022-01-04", "FB", "0001326801"),
    snap("2023-01-04", "META", "0001326801"), snap("2024-01-04", "META", "0001326801"),
    snap("2025-01-04", "META", "0001326801"), snap("2026-01-04", "META", "0001326801"),
]
INDEX = {  # what the EODHD cache holds: the BBBY series is Overstock's, spliced under the code
    "BBBY": ("2002-05-30", "2026-09-04"), "BBBYQ": ("1992-06-05", "2023-09-29"),
    "OSTK": ("2002-05-30", "2024-03-20"), "BYON": ("2002-05-30", "2025-08-28"),
    "FB": ("2012-05-18", "2022-06-08"), "META": ("2012-05-18", "2026-09-04"),
}


GOOGLE, ALPHABET = "0001288776", "0001652044"
SNAPS += [
    snap("2021-01-04", "GOOG", GOOGLE), snap("2022-01-04", "GOOG", ALPHABET),
    snap("2023-01-04", "GOOG", ALPHABET), snap("2024-01-04", "GOOG", ALPHABET),
    snap("2025-01-04", "GOOG", ALPHABET), snap("2026-01-04", "GOOG", ALPHABET),
]
INDEX["GOOG"] = ("2004-08-19", "2026-09-04")


@pytest.fixture
def master():
    return sm.SecurityMaster(SNAPS, INDEX)


def test_reused_ticker_resolves_by_date(master):
    assert master.resolve("BBBY", "2022-01-04").cik == OLD
    assert master.resolve("BBBY", "2026-01-04").cik == NEW
    assert master.resolve("BBBY", "2022-01-04").confidence == "exact"


def test_between_snapshots_flags_a_company_that_vanished(master):
    r = master.resolve("BBBY", "2023-08-16")
    assert r.cik == OLD and r.confidence == "changed_between"


def test_rename_keeps_the_same_company(master):
    assert master.resolve("FB", "2021-06-01").cik == master.resolve("META", "2024-06-01").cik
    assert master.resolve("FB", "2021-06-01").confidence == "stable"


def test_identity_never_comes_from_the_future(master):
    # BBBY at mid-2025: the 2026 snapshot gives it to Overstock's successor, a company that
    # already existed (as BYON). That is a later rename, not a listing, so it must not leak.
    r = master.resolve("BBBY", "2025-06-01")
    assert r.cik is None and r.confidence == "unknown"


def test_unknown_ticker(master):
    assert master.resolve("ZZZZ", "2022-01-04").confidence == "unknown"


def test_lifetime_end(master):
    assert master.lifetime_end(OLD) == "2024-01-04"
    assert master.lifetime_end(NEW) is None  # still in the newest snapshot


def test_old_bbby_gets_bbbyq_not_the_spliced_bbby_series(master):
    # the BBBY series runs to 2026, years past the old company's life: it is Overstock's
    assert master.price_code(OLD, "2022-08-16") == ("BBBYQ", "ok")


def test_overstock_prefers_the_ticker_it_held_then(master):
    assert master.price_code(NEW, "2022-08-16") == ("OSTK", "ok")
    assert master.price_code(NEW, "2025-06-01")[0] == "BYON"


def test_rename_uses_the_one_series(master):
    fb = master.resolve("FB", "2021-06-01").cik
    assert master.price_code(fb, "2021-06-01")[0] in {"FB", "META"}


def test_no_series_covering_the_date_is_reported(master):
    code, why = master.price_code(OLD, "2024-06-01")  # after BBBYQ ends
    assert code is None and why.startswith("no_valid_series")


def test_unknown_cik(master):
    assert master.price_code("000", "2022-01-01") == (None, "unknown_cik")


def test_successor_carries_the_same_series_across_a_cik_change(master):
    # GOOG sits in consecutive snapshots under a new CIK: same listing, so the series is
    # the old company's too (unlike BBBY, which vanished for two snapshots first)
    assert master.lifetime_end(GOOGLE) is None
    assert master.price_code(GOOGLE, "2020-06-01") == ("GOOG", "ok")


def test_reuse_after_a_gap_is_not_a_successor(master):
    assert master.lifetime_end(OLD) == "2024-01-04"


def test_otc_tail_past_the_exchange_listing_is_still_the_companys_series():
    snaps = [snap("2021-01-04", "SUNE", "1"), snap("2022-01-04", "OTHR", "2"),
             snap("2023-01-04", "OTHR", "2")]
    idx = {"SUNE": ("2005-01-01", "2022-09-01"), "SUNEQ": ("2016-04-21", "2022-09-01")}
    m = sm.SecurityMaster(snaps, idx)
    # SUNE vanished from the listings in 2022 but its Q-suffixed OTC series runs on
    assert m.lifetime_end("1") == "2022-01-04"
    assert m.price_code("1", "2021-06-01")[0] == "SUNE"
    assert m.price_code("1", "2022-06-01")[0] in {"SUNE", "SUNEQ"}


def test_code_taken_over_by_a_new_company_is_rejected_even_without_overhang():
    snaps = [snap("2021-01-04", "SPWR", "1"), snap("2022-01-04", "X", "3"),
             snap("2023-01-04", "SPWR", "2")]
    m = sm.SecurityMaster(snaps, {"SPWR": ("2005-01-01", "2023-06-01")})
    assert m.price_code("1", "2021-06-01") == (None, "no_valid_series (SPWR reused by another company)")
    assert m.price_code("2", "2023-02-01")[0] == "SPWR"


def test_new_listing_between_snapshots_is_accepted_but_a_later_rename_is_not():
    snaps = [snap("2020-01-04", "FB", "8"), snap("2022-01-04", "META", "8"),
             snap("2022-01-04", "PLTR", "2")]
    m = sm.SecurityMaster(snaps)
    ipo = m.resolve("PLTR", "2020-06-01")
    assert ipo.cik == "2" and ipo.confidence == "from_later_snapshot"
    assert m.resolve("META", "2020-06-01").confidence == "unknown"  # FB existed: renamed later


def test_fill_types_uses_other_snapshots_then_eodhd():
    rows = [
        {"as_of": "2012-01-04", "ticker": "AAA", "cik": "1", "type": None},     # typed later
        {"as_of": "2018-01-04", "ticker": "AAA", "cik": "1", "type": "CS"},
        {"as_of": "2012-01-04", "ticker": "DEAD", "cik": "2", "type": None},    # never typed
        {"as_of": "2012-01-04", "ticker": "FUNDX", "cik": "3", "type": None},   # not in EODHD
        {"as_of": "2012-01-04", "ticker": "SPY", "cik": "4", "type": None},
    ]
    out = {r["ticker"]: r["type"] for r in sm.fill_types(
        rows, {"DEAD": "Common Stock", "SPY": "ETF"})}
    assert out == {"AAA": "CS", "DEAD": "CS", "FUNDX": None, "SPY": "ETF"}


# ---------------------------------------------------------------- name check on price series

def test_a_held_ticker_whose_series_is_another_company_is_rejected():
    # DCP Midstream held "DPM" in 2017; EODHD's DPM is a different, tiny company, and the real
    # history sits under today's code DCP
    snaps = [snap("2017-01-04", "DPM", "9", "DCP Midstream Partners, LP"),
             snap("2018-01-04", "DCP", "9", "DCP Midstream LP"),
             snap("2019-01-04", "DCP", "9", "DCP Midstream LP")]
    idx = {"DPM": ("2008-01-01", "2024-01-01"), "DCP": ("2005-12-02", "2019-06-01")}
    names = {"DPM": "Dundee Precious Metals Inc", "DCP": "DCP Midstream LP"}
    m = sm.SecurityMaster(snaps, idx, names)
    code, why = m.price_code("9", "2017-06-01")
    assert code == "DCP" and why == "ok"


def test_without_symbol_names_the_check_is_skipped():
    snaps = [snap("2017-01-04", "DPM", "9", "DCP Midstream Partners, LP"),
             snap("2018-01-04", "DPM", "9", "DCP Midstream LP")]
    m = sm.SecurityMaster(snaps, {"DPM": ("2008-01-01", "2024-01-01")})
    assert m.price_code("9", "2017-06-01")[0] == "DPM"


def test_if_every_candidate_is_the_wrong_company_there_is_no_series():
    snaps = [snap("2017-01-04", "ZZZ", "9", "Acme Widgets Inc"), snap("2018-01-04", "ZZZ", "9", "Acme Widgets Inc")]
    m = sm.SecurityMaster(snaps, {"ZZZ": ("2008-01-01", "2024-01-01")}, {"ZZZ": "Totally Different Corp"})
    code, why = m.price_code("9", "2017-06-01")
    assert code is None and "different company" in why


def test_previous_holder_of_a_reused_code_is_found_under_old_suffix():
    snaps = [snap("2010-01-04", "DCP", "5", "DynCorp International Inc"),
             snap("2018-01-04", "DCP", "6", "DCP Midstream LP")]
    idx = {"DCP": ("2005-12-02", "2023-06-16"), "DCP_old": ("2006-05-04", "2010-07-07")}
    names = {"DCP": "DCP Midstream LP", "DCP_old": "Dyncorp International Inc"}
    m = sm.SecurityMaster(snaps, idx, names)
    assert m.price_code("5", "2008-01-01")[0] == "DCP_old"
    assert m.price_code("6", "2019-01-01")[0] == "DCP"


# --------------------------------------------------------------------------- #
# export-tickers: the download filter's list must include delisted names
# --------------------------------------------------------------------------- #


def test_ticker_base_strips_share_classes_and_rejects_non_tickers():
    assert sm.ticker_base("BRK.A") == "BRK" and sm.ticker_base("brk-b") == "BRK"
    assert sm.ticker_base("GOOGL") == "GOOGL"
    assert sm.ticker_base("TOOLONG") is None and sm.ticker_base("AB12") is None
    assert sm.ticker_base("") is None and sm.ticker_base(None) is None


def test_all_tickers_unions_every_snapshot_date_and_eodhd_stock_codes():
    snaps = [rec("RSH", "1", as_of="2012-01-04", active=False),   # delisted long ago
             rec("AAPL", "2", as_of="2012-01-04"), rec("AAPL", "2", as_of="2026-01-04"),
             rec("BRK.B", "3", as_of="2026-01-04")]
    symbols = [{"code": "WLT", "type": "Common Stock"}, {"code": "SPY", "type": "ETF"},
               {"code": "XYZ-P", "type": "Preferred Stock"}]
    got = sm.all_tickers(snaps, symbols)
    assert got == {"RSH", "AAPL", "BRK", "WLT", "SPY"}  # preferred excluded, classes stripped


def test_export_tickers_command_writes_a_filter_list(tmp_path, capsys):
    import pyarrow as pa
    import pyarrow.parquet as pq
    root = tmp_path / "massive"
    (root / "snapshots").mkdir(parents=True)
    for d, rows in (("2012-01-04", [rec("RSH", "1"), rec("AAPL", "2")]),
                    ("2026-01-04", [rec("AAPL", "2")])):
        pq.write_table(pa.Table.from_pylist([{**r, "as_of": d} for r in rows]),
                       root / "snapshots" / f"{d}.parquet")
    sym = tmp_path / "symbols.parquet"
    pq.write_table(pa.Table.from_pylist([{"code": "WLT", "type": "Common Stock"}]), sym)
    dest = tmp_path / "ref" / "tickers_all.txt"
    assert sm.main(["--out", str(root), "export-tickers", "--symbols", str(sym),
                    "--dest", str(dest)]) == 0
    lines = dest.read_text().splitlines()
    assert lines[0].startswith("# 3 tickers") and lines[1:] == ["AAPL", "RSH", "WLT"]
    assert "3 tickers" in capsys.readouterr().out


def test_the_member_ticker_wins_over_another_product_of_the_same_issuer():
    # JPMorgan's CIK also lists the AMJ ETN; alphabetically AMJ came first and every JPM row
    # in the universe was the ETN (found 2026-10-27)
    snaps = [snap("2020-01-02", "JPM", "19617"), snap("2020-01-02", "AMJ", "19617"),
             snap("2021-01-04", "JPM", "19617"), snap("2021-01-04", "AMJ", "19617")]
    idx = {"JPM": ("1990-01-01", "2026-10-01"), "AMJ": ("2009-04-01", "2026-10-01")}
    m = sm.SecurityMaster(snaps, idx)
    assert m.price_code("19617", "2020-06-01", "JPM") == ("JPM", "ok")
    assert m.price_code("19617", "2020-06-01")[0] == "AMJ"          # the old behaviour without the hint
