"""Export the Reddit darlings for the thread browser's timeline: the events, their posts, prices.

    python scripts/darling_export.py [--variant identified] [--push]

Reads the crowd-rule result written by ``darlings.py run --rule crowd`` (``data/experiments/
darlings_crowd_<split>_<variant>.json``: every darling event with the bullish threads that
made it one), the thesis_v2 grades of those threads (``--grades-dir``), the text the model saw
(from the LLM cache, as ``thread_export.py`` does), each event's 1/3/5-year outcome from the
labels, and for every darling stock its weekly adjusted close from a year before its first
darling post to the end of the price data, plus the close on each post's day for the chart's
markers. Output: ``data/ui/darlings.json.zst``; ``--push`` copies it to the bucket, where
``thread_server.py`` reads it for ``/`` (the timeline) and ``/stock/<TICKER>`` (the chart). Like
the thread export it holds Reddit text: bucket and password-gated service only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prices as px  # noqa: E402
import thesis_scorer as ts  # noqa: E402
import thread_export as te  # noqa: E402

KEY = "data/ui/darlings.json.zst"
HORIZONS = (1, 3, 5)
WEEK = 7


def outcomes(labels: Path, pairs: set) -> dict[tuple[str, str, int], dict]:
    """(month, cik, years) -> outcome, for the event company-months only."""
    if not labels.exists() or not pairs:
        return {}
    import duckdb
    import pyarrow as pa
    con = duckdb.connect()
    con.register("want", pa.Table.from_pylist([{"month": m, "cik": c} for m, c in pairs]))
    cols = ", ".join(f"l.{k}" for k in te.OUTCOME_KEYS)
    return {(m, c, int(y)): dict(zip(te.OUTCOME_KEYS, rest)) for m, c, y, *rest in con.execute(
        f"select l.month, l.cik, l.years, {cols} from read_parquet('{labels}') l "
        "join want w on w.month = l.month and w.cik = l.cik").fetchall()}


def codes_and_names(universe: Path, symbols: Path, pairs: set) -> tuple[dict, dict]:
    """(month, cik) -> price code that month; code -> EODHD's current name for it."""
    if not universe.exists() or not pairs:
        return {}, {}
    import duckdb
    import pyarrow as pa
    con = duckdb.connect()
    con.register("want", pa.Table.from_pylist([{"month": m, "cik": c} for m, c in pairs]))
    code = {(m, c): k for m, c, k in con.execute(
        f"select w.month, w.cik, u.code from want w join read_parquet('{universe}') u "
        "on u.month = w.month and u.cik = w.cik where u.code is not null").fetchall()}
    names = {}
    if symbols.exists():
        for r in pq.read_table(symbols, columns=["code", "name"]).to_pylist():
            names.setdefault(r["code"], r["name"])
    return code, names


def weekly(s: px.Series, start_day: int) -> list[list]:
    """[[YYYY-MM-DD, adjusted close]] every WEEK days from ``start_day`` to the last bar, the
    last bar on or before each step (the last bar always included)."""
    out, d = [], start_day
    last = int(s.days[-1])
    while d <= last:
        i = s.index_on_or_before(d)
        if i is not None and (not out or out[-1][0] != _iso(s.days[i])):
            out.append([_iso(s.days[i]), round(float(s.close[i]), 4)])
        d += WEEK
    if out and out[-1][0] != _iso(s.days[-1]):
        out.append([_iso(s.days[-1]), round(float(s.close[-1]), 4)])
    return out


def _iso(day) -> str:
    return str((px.DAY0 + np.timedelta64(int(day), "D")).astype("datetime64[D]"))


def build(events: list[dict], grades: list[dict], llm_dir: Path, variant: str, labels: Path, universe: Path,
          symbols: Path, eodhd: Path, log=print) -> dict:
    by_key = {(g["post_id"], g["cik"]): g for g in grades}
    want = [by_key[(pid, e["cik"])] for e in events for pid in e["threads"] if (pid, e["cik"]) in by_key]
    seen, uniq = set(), []
    for g in want:
        if (g["post_id"], g["cik"]) not in seen:
            seen.add((g["post_id"], g["cik"]))
            uniq.append(g)
    threads = te.export(uniq, llm_dir, variant, {}, log=log)
    pairs = {(e["month"], e["cik"]) for e in events}
    outs = outcomes(labels, pairs)
    code, names = codes_and_names(universe, symbols, pairs)
    store = px.PriceStore(eodhd)
    first_post: dict[str, int] = {}
    for g in uniq:
        if g.get("created_utc"):
            d = int(g["created_utc"]) // 86400
            first_post[g["cik"]] = min(first_post.get(g["cik"], d), d)
    stocks, evs = {}, []
    for e in events:
        k = code.get((e["month"], e["cik"]))
        ticker = e.get("ticker") or k or e["cik"]
        evs.append({"ticker": ticker, "cik": e["cik"], "month": e["month"],
                    "name": names.get(k) or e.get("name"), "code": k,
                    "bulls": e.get("bulls"), "bears": e.get("bears"), "authors": e.get("authors"),
                    "comments": e.get("comments"), "credibility": e.get("credibility"),
                    "bull_weight": e.get("bull_weight"), "bear_weight": e.get("bear_weight"), "late": e.get("late"),
                    "keys": [f"{pid}_{e['cik']}" for pid in e["threads"] if (pid, e["cik"]) in by_key],
                    "outcome": {str(y): outs.get((e["month"], e["cik"], y)) for y in HORIZONS}})
        if ticker in stocks or not k:
            continue
        s = store.get(k)
        if s is None:
            continue
        start = min(first_post.get(e["cik"], px.to_day(e["month"])), px.to_day(e["month"])) - 365
        stocks[ticker] = {"code": k, "name": names.get(k) or e.get("name"), "cik": e["cik"],
                          "prices": weekly(s, max(start, int(s.days[0])))}
    # each darling post's close on its day, for the chart's markers
    for r in threads:
        ev = next((x for x in evs if r["key"] in x["keys"]), None)
        st = stocks.get(ev["ticker"]) if ev else None
        r["darling_ticker"] = ev["ticker"] if ev else r.get("ticker")
        r["close_at_post"] = None
        if st and r.get("created_utc"):
            s = store.get(st["code"])
            i = s.index_on_or_before(int(r["created_utc"]) // 86400) if s is not None else None
            if i is not None:
                r["close_at_post"] = round(float(s.close[i]), 4)
    log(f"[darling-export] {len(evs)} darling events over {len({e['ticker'] for e in evs})} stocks, "
        f"{len(threads)} darling posts, price charts for {len(stocks)} stocks")
    return {"events": evs, "threads": threads, "stocks": stocks}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--variant", default="identified")
    p.add_argument("--split", default="recent")
    p.add_argument("--prompt", default=os.environ.get("THESIS_PROMPT") or "thesis_v2")
    p.add_argument("--grades-dir", default="data/features/darlings_b")
    p.add_argument("--result", default=None, help="default data/experiments/darlings_crowd_<split>_<variant>.json")
    p.add_argument("--features", default="data/features")
    p.add_argument("--symbols", default="data/market/eodhd/symbols.parquet")
    p.add_argument("--eodhd", default="data/market/eodhd")
    p.add_argument("--llm-dir", default="data/llm")
    p.add_argument("--out", default=KEY)
    p.add_argument("--push", action="store_true", help="copy the file to the bucket under the same key")
    a = p.parse_args(argv)
    res_path = Path(a.result or f"data/experiments/darlings_crowd_{a.split}_{a.variant}.json")
    res = json.loads(res_path.read_text())
    grades = ts.load_grades(Path(a.grades_dir), a.variant, a.prompt)
    feats = Path(a.features)
    data = build(res["events"], grades, Path(a.llm_dir), a.variant, feats / "labels.parquet",
                 feats / "universe.parquet", Path(a.symbols), Path(a.eodhd))
    data.update({"rule": res.get("level"), "runup": res.get("runup"), "blue_chip_top": res.get("blue_chip_top"),
                 "counts": res.get("counts"), "summary": res.get("outcomes"), "variant": a.variant,
                 "split": a.split, "source": str(res_path)})
    blob = zstd.ZstdCompressor(level=9).compress(json.dumps(data, ensure_ascii=False, default=str).encode())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(blob)
    print(f"[darling-export] wrote {out} ({len(blob) / 1e6:.2f} MB compressed)", flush=True)
    if a.push:
        import bucket_sync as bs
        bs.Bucket(bs.load_creds(), workers=2).put_bytes(str(out), blob)
        print(f"[darling-export] pushed to the bucket as {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
