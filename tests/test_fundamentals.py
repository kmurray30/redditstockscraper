import gzip
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import fundamentals as fd  # noqa: E402


def f(start, end, val, filed):
    r = {"end": end, "val": val, "filed": filed}
    if start:
        r["start"] = start
    return r


FACTS = {"facts": {
    "us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            f(None, "2022-12-31", 300e6, "2023-02-28"), f(None, "2023-12-31", 200e6, "2024-02-28"),
            f(None, "2024-03-31", 180e6, "2024-05-10")]}},
        "LongTermDebt": {"units": {"USD": [f(None, "2024-03-31", 350e6, "2024-05-10")]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            f("2023-01-01", "2023-12-31", -120e6, "2024-02-28"), f("2023-01-01", "2023-03-31", -40e6, "2023-05-10"),
            f("2024-01-01", "2024-03-31", -20e6, "2024-05-10")]}},
        "Revenues": {"units": {"USD": [
            f("2022-01-01", "2022-12-31", 200e6, "2023-02-28"), f("2023-01-01", "2023-12-31", 240e6, "2024-02-28"),
            f("2023-01-01", "2023-03-31", 50e6, "2023-05-10"), f("2024-01-01", "2024-03-31", 70e6, "2024-05-10")]}}},
    "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        f(None, "2023-04-30", 100e6, "2023-05-10"), f(None, "2024-04-30", 480e6, "2024-05-10")]}}}}}


def test_point_in_time_metrics_ttm_and_split_adjusted_dilution():
    # before the Q1 2024 filing: the 2023 annual numbers only
    m = fd.metrics(FACTS, "2024-03-01")
    assert m["cash"] == 200e6 and m["ocf_ttm"] == -120e6 and m["revenue_ttm"] == 240e6 and m["debt"] == 0
    assert m["revenue_ttm_prev"] == 200e6 and abs(m["revenue_growth"] - 0.2) < 1e-9 and m["shares"] == 100e6
    # after it: TTM = FY2023 + Q1 2024 - Q1 2023
    m = fd.metrics(FACTS, "2024-06-01", splits=[("2024-01-15", 4.0)])
    assert m["ocf_ttm"] == -120e6 - 20e6 + 40e6 and m["revenue_ttm"] == 240e6 + 70e6 - 50e6
    assert m["debt"] == 350e6 and abs(m["runway_years"] - 1.8) < 1e-9
    assert abs(m["dilution"] - (480 / 400 - 1)) < 1e-9           # a 4:1 split, then 20% more shares
    status, why = fd.gate(m)
    assert status == "fail" and any("runway" in w for w in why) and any("debt" in w for w in why)
    assert fd.gate(fd.metrics(None, "2024-06-01"))[0] == "unknown"
    ok = dict(m, runway_years=math.inf, ocf_ttm=50e6, debt=10e6, dilution=0.05, revenue_growth=0.3)
    assert fd.gate(ok) == ("pass", []) and "cash-flow positive" in fd.describe(ok)


def test_pull_saves_and_skips(tmp_path):
    calls = []
    def fetcher(cik, ua):
        calls.append(cik)
        return json.dumps(FACTS).encode() if cik.endswith("1") else None
    fd.pull(["1", "2"], tmp_path, "ua", fetcher=fetcher, sleep=lambda s: None, log=lambda m: None)
    assert (tmp_path / "0000000001.json.gz").exists() and (tmp_path / "0000000002.missing").exists()
    fd.pull(["1", "2"], tmp_path, "ua", fetcher=fetcher, sleep=lambda s: None, log=lambda m: None)
    assert len(calls) == 2
    assert fd.Facts(tmp_path).get("1")["facts"]["dei"] and fd.Facts(tmp_path).get("2") is None
    assert json.loads(gzip.decompress((tmp_path / "0000000001.json.gz").read_bytes()))


def test_latest_concept_wins_dual_classes_add_and_stale_revenue_is_not_growth():
    facts = {"facts": {"us-gaap": {
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [f(None, "2020-01-26", 10e9, "2020-02-20")]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [f("2019-01-28", "2020-01-26", 4.7e9, "2020-02-20")]}},
        "Revenues": {"units": {"USD": [f("2016-01-31", "2017-01-29", 6.9e9, "2017-03-01"),
                                       f("2017-01-30", "2018-01-28", 9.7e9, "2018-02-28")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            f("2018-01-29", "2019-01-27", 11.7e9, "2019-02-21"), f("2019-01-28", "2020-01-26", 10.9e9, "2020-02-20")]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2020-02-14", "val": 600e6, "filed": "2020-02-20", "accn": "x"},
            {"end": "2020-02-14", "val": 100e6, "filed": "2020-02-20", "accn": "x"},
            {"end": "2019-02-15", "val": 690e6, "filed": "2019-02-21", "accn": "y"}]}}}}}
    m = fd.metrics(facts, "2020-04-01")
    assert m["revenue_ttm"] == 10.9e9 and abs(m["revenue_growth"] - (10.9 / 11.7 - 1)) < 1e-9   # not -22% from Revenues
    assert m["shares"] == 700e6 and abs(m["dilution"] - (700 / 690 - 1)) < 1e-9                  # class A + B
    old = {"facts": {"us-gaap": {k: v for k, v in facts["facts"]["us-gaap"].items() if k != "RevenueFromContractWithCustomerExcludingAssessedTax"}}}
    assert fd.metrics(old, "2020-04-01")["revenue_growth"] is None        # FY2018 revenue is two years stale


def test_quarters_and_years_under_different_concepts_are_pooled():
    rev = {"Revenues": {"units": {"USD": [
        f("2019-01-28", "2019-07-28", 4.799e9, "2019-08-15"), f("2020-01-27", "2020-07-26", 6.946e9, "2020-08-19")]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            f("2018-01-29", "2019-01-27", 11.716e9, "2019-02-21"), f("2019-01-28", "2020-01-26", 10.918e9, "2020-02-20")]}}}
    v, end = fd.ttm({"facts": {"us-gaap": rev}}, fd.REVENUE, "2020-09-01")
    assert end == "2020-07-26" and abs(v - (10.918e9 + 6.946e9 - 4.799e9)) < 1


def test_bulk_pull_resumes_and_facts_read_from_the_zip(tmp_path):
    import io
    import json as _json
    import zipfile as _zip
    buf = io.BytesIO()
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("CIK0000000042.json", _json.dumps({"cik": 42, "facts": {}}))
    blob = buf.getvalue()

    class Resp:
        def __init__(self, body, status, headers):
            self.body, self.status, self.headers = io.BytesIO(body), status, headers

        def read(self, n=-1):
            return self.body.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    out = tmp_path / "companyfacts.zip"
    part = out.with_name(out.name + ".part")
    part.write_bytes(blob[:10])                       # an interrupted earlier download

    def opener(req, timeout=0):
        start = int(req.headers["Range"].split("=")[1].rstrip("-"))
        rest = blob[start:]
        return Resp(rest, 206, {"Content-Range": f"bytes {start}-{len(blob) - 1}/{len(blob)}",
                                "Content-Length": str(len(rest))})

    assert fd.bulk_pull(out, "ua", opener=opener, log=lambda *_: None) == len(blob)
    assert out.read_bytes() == blob and not part.exists()
    f = fd.Facts(tmp_path / "none", bulk=out)
    assert f.get("42") == {"cik": 42, "facts": {}} and f.get("43") is None


def test_profile_picks_basis_and_currency_from_the_filings():
    asml = {"facts": {"us-gaap": {"Revenues": {"units": {"EUR": [f("2023-01-01", "2023-12-31", 1, "2024-02-01")] * 3}}}}}
    sap = {"facts": {"ifrs-full": {"Revenue": {"units": {"EUR": [f("2023-01-01", "2023-12-31", 1, "2024-02-01")] * 3,
                                                           "USD": [f("2017-01-01", "2017-12-31", 1, "2018-02-01")]}}}}}
    assert fd.profile_for(FACTS) == (fd.GAAP, "USD")
    assert fd.profile_for(asml) == (fd.GAAP, "EUR")
    assert fd.profile_for(sap) == (fd.IFRS, "EUR")
    assert fd.profile_for(None) == (fd.GAAP, "USD")


def test_panel_rows_are_point_in_time_with_market_cap(tmp_path):
    import io
    import zipfile as _zip
    import pyarrow as pa
    import pyarrow.parquet as pq
    bulk = tmp_path / "companyfacts.zip"
    with _zip.ZipFile(bulk, "w") as z:
        z.writestr("CIK0000000001.json", json.dumps(FACTS))
    uni = tmp_path / "universe.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"month": m, "cik": "1", "ticker": "A", "code": "A", "price": 10.0, "eligible": True}
        for m in ("2024-03-01", "2024-05-10", "2024-06-01")]), uni)
    out = tmp_path / "fundamentals.parquet"
    n = fd.build_panel(uni, out, bulk, tmp_path / "none", tmp_path / "eodhd", workers=1, log=lambda *_: None)
    rows = {r["month"]: r for r in pq.read_table(out).to_pylist()}
    assert n == 3
    # 2024-03-01: the 2023 annual report only; shares 100M on 2023-04-30 -> $1B at $10
    assert rows["2024-03-01"]["revenue_ttm"] == 240e6 and rows["2024-03-01"]["mcap"] == 1e9
    assert rows["2024-03-01"]["last_filed"] is None   # these facts carry no form
    # the Q1 report was filed on 2024-05-10: a cutoff that day must not see it, the next month does
    assert rows["2024-05-10"]["revenue_ttm"] == 240e6
    assert rows["2024-06-01"]["revenue_ttm"] == 260e6 and rows["2024-06-01"]["mcap"] == 4.8e9
    assert {r["health"] for r in rows.values()} <= {"pass", "fail", "unknown"}


def test_fixes_found_by_the_spot_checks():
    import copy
    # net income and other extra measures read from the company's own namespace, not "dei"
    g = copy.deepcopy(FACTS)
    g["facts"]["us-gaap"]["NetIncomeLoss"] = {"units": {"USD": [f("2023-01-01", "2023-12-31", 30e6, "2024-02-28")]}}
    m = fd.metrics(g, "2024-03-01")
    assert m["net_income_ttm"] == 30e6 and m["basis"] == "us-gaap"
    # no cover-page shares (dimensional only in the bulk file): the weighted average stands in
    del g["facts"]["dei"]
    g["facts"]["us-gaap"]["WeightedAverageNumberOfSharesOutstandingBasic"] = {"units": {"shares": [
        f("2023-10-01", "2023-12-31", 390e6, "2024-02-28"), f("2022-10-01", "2022-12-31", 380e6, "2023-02-28")]}}
    m = fd.metrics(g, "2024-03-01")
    assert m["shares"] == 390e6 and abs(m["dilution"] - (390 / 380 - 1)) < 1e-9
    # a share count older than SHARES_MAX_AGE is not used
    assert fd.shares_at(g, "2025-06-01") is None
    # a balance sheet from years ago is unknown, not health
    m = fd.metrics(FACTS, "2026-06-01")
    assert not m["known"] and "balance sheet" in m["why"] and fd.gate(m)[0] == "unknown"
    # currency by the latest filing year first: Nebius moved from roubles to dollars
    nbis = {"facts": {"us-gaap": {"Revenues": {"units": {
        "RUB": [f("2019-01-01", "2019-12-31", 1, "2020-03-01")] * 9,
        "USD": [f("2024-01-01", "2024-12-31", 1, "2025-03-01")] * 2}}}}}
    assert fd.profile_for(nbis) == (fd.GAAP, "USD")
    assert fd.describe(dict(m, known=True, runway_years=math.inf, cash=1e9, debt=0, currency="EUR")).startswith("cash EUR")
