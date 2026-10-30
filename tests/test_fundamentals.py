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
