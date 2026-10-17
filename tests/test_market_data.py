import importlib.util
import json
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq
import pytest

spec = importlib.util.spec_from_file_location(
    "market_data", Path(__file__).parents[1] / "scripts" / "market_data.py"
)
md = importlib.util.module_from_spec(spec)
spec.loader.exec_module(md)

KEY = "sekrit-key-123"


class FakeHttp:
    """Maps a URL fragment to a queue of (status, json) responses."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for frag, queue in self.routes.items():
            if frag in url:
                status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
                return status, json.dumps(payload).encode()
        return 404, b"[]"


def client(routes, **kw):
    http = FakeHttp(routes)
    return md.Eodhd(KEY, opener=http, sleep=lambda s: None, **kw), http


BARS = [
    {"date": "2012-01-04", "open": 2, "high": 3, "low": 1, "close": 2.5,
     "adjusted_close": 0.5, "volume": 10},
    {"date": "2012-01-03", "open": 1, "high": 2, "low": 1, "close": 1.5,
     "adjusted_close": 0.4, "volume": 9},
]


def test_retries_rate_limit_then_succeeds():
    c, http = client({"eod/AAA.US": [(429, {}), (503, {}), (200, BARS)]})
    assert c.get("eod/AAA.US") == BARS
    assert len(http.calls) == 3


def test_error_message_never_contains_the_key():
    c, _ = client({"eod/AAA.US": [(403, {"message": "nope"})]})
    with pytest.raises(md.EodhdError) as e:
        c.get("eod/AAA.US")
    assert KEY not in str(e.value)


def test_unknown_symbol_is_empty_not_error():
    c, _ = client({})
    assert c.get("eod/ZZZ.US") == []


def test_fetch_one_sorts_ascending_and_is_resumable(tmp_path):
    c, http = client({"eod/AAA.US": [(200, BARS)]})
    assert md.fetch_one(c, tmp_path, "prices", "AAA") == "ok"
    t = pq.read_table(tmp_path / "eod" / "AAA.parquet")
    assert t.column("date").to_pylist() == ["2012-01-03", "2012-01-04"]
    n = len(http.calls)
    assert md.fetch_one(c, tmp_path, "prices", "AAA") == "cached"
    assert len(http.calls) == n  # no second request


def test_empty_result_is_cached_so_it_is_not_refetched(tmp_path):
    c, http = client({"eod/NADA.US": [(200, [])]})
    assert md.fetch_one(c, tmp_path, "prices", "NADA") == "empty"
    assert md.fetch_one(c, tmp_path, "prices", "NADA") == "cached"
    assert len(http.calls) == 1


def test_splits_become_ratios(tmp_path):
    c, _ = client({"splits/AAPL.US": [(200, [
        {"date": "2020-08-31", "split": "4.000000/1.000000"},
        {"date": "2014-06-09", "split": "7.000000/1.000000"},
        {"date": "2000-01-01", "split": "garbage"}])]})
    md.fetch_one(c, tmp_path, "splits", "AAPL")
    t = pq.read_table(tmp_path / "splits" / "AAPL.parquet").to_pylist()
    assert t == [{"date": "2014-06-09", "ratio": 7.0}, {"date": "2020-08-31", "ratio": 4.0}]


def test_reverse_split_ratio_below_one(tmp_path):
    c, _ = client({"splits/X.US": [(200, [{"date": "2019-01-01", "split": "1.0/10.0"}])]})
    md.fetch_one(c, tmp_path, "splits", "X")
    assert pq.read_table(tmp_path / "splits" / "X.parquet").to_pylist()[0]["ratio"] == 0.1


def test_dividends_rename_camel_case_fields(tmp_path):
    c, _ = client({"div/AAPL.US": [(200, [{
        "date": "2012-08-09", "declarationDate": "2012-07-24", "recordDate": "2012-08-13",
        "paymentDate": "2012-08-16", "period": "Quarterly", "value": 0.0946,
        "unadjustedValue": 2.6488, "currency": "USD"}])]})
    md.fetch_one(c, tmp_path, "dividends", "AAPL")
    row = pq.read_table(tmp_path / "dividends" / "AAPL.parquet").to_pylist()[0]
    assert row["unadjusted_value"] == 2.6488 and row["record_date"] == "2012-08-13"


def test_odd_codes_get_safe_filenames(tmp_path):
    p = md.cache_path(tmp_path, "eod", "BRK/B")
    assert p.parent == tmp_path / "eod" and "/" not in p.name


def symbols(rows):
    import pyarrow as pa
    return pa.Table.from_pylist([
        {"code": c, "exchange": e, "type": t, "delisted": d, "name": None, "isin": None,
         "currency": "USD", "pulled_at": ""} for c, e, t, d in rows])


def test_work_order_mentioned_first_then_major_then_otc():
    sym = symbols([
        ("OTCX", "PINK", "Common Stock", False),
        ("BIG", "NYSE", "Common Stock", False),
        ("RARE", "PINK", "Common Stock", True),   # mentioned, so ahead of BIG
        ("POP", "NASDAQ", "Common Stock", False),  # mentioned more than RARE
        ("FND", "NASDAQ", "FUND", False),          # funds are skipped
        ("BIG", "NYSE", "Common Stock", True),     # duplicate code appears once
    ])
    order = md.work_order(sym, Counter({"POP": 50, "RARE": 3, "ZZZ": 9}))
    assert order == ["POP", "RARE", "BIG", "OTCX"]


def test_work_order_can_drop_otc():
    sym = symbols([("A", "NYSE", "ETF", False), ("B", "PINK", "Common Stock", False)])
    assert md.work_order(sym, Counter(), include_otc=False) == ["A"]


def test_mentions_are_summed_over_subreddits(tmp_path):
    for sub, n in (("investing", 2), ("stocks", 3)):
        d = tmp_path / sub
        d.mkdir()
        (d / "mentions_posts.csv").write_text(
            f"date,subreddit,ticker,source,count\n2020-01-01,{sub},AAPL,post,{n}\n")
    assert md.mentioned_tickers(tmp_path) == Counter({"AAPL": 5})


def test_pull_counts_outcomes_and_survives_an_error(tmp_path):
    c, _ = client({"eod/GOOD.US": [(200, BARS)], "eod/BAD.US": [(403, {})]}, retries=0)
    stats = md.pull(c, tmp_path, "prices", ["GOOD", "BAD", "GOOD"], workers=1, log=lambda m: None)
    assert stats["ok"] == 1 and stats["error"] == 1 and stats["cached"] == 1


def test_load_key_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nEODHD_API_KEY='abc'\n")
    assert md.load_key(env) == "abc"


def test_price_index_reports_first_last_and_skips_empty(tmp_path):
    c, _ = client({"eod/AAA.US": [(200, BARS)], "eod/NADA.US": [(200, [])]})
    md.fetch_one(c, tmp_path, "prices", "AAA")
    md.fetch_one(c, tmp_path, "prices", "NADA")
    rows = md.build_price_index(tmp_path).to_pylist()
    assert rows == [{"code": "AAA", "first_date": "2012-01-03", "last_date": "2012-01-04",
                     "bars": 2}]
    md.write_atomic(md.build_price_index(tmp_path), tmp_path / "eod_index.parquet")
    assert md.load_price_index(tmp_path) == {"AAA": ("2012-01-03", "2012-01-04")}
