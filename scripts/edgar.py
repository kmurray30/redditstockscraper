#!/usr/bin/env python3
"""SIC industry codes from SEC EDGAR, for sector-matched baselines.

SEC fair-access rules require a User-Agent that identifies you. Set ``SEC_USER_AGENT`` in the
environment or ``.env`` (e.g. ``"Your Name your.email@example.com"``); this script refuses
to run without it and never makes one up.

    data/ref/sic.parquet   cik, sic, sic_description, division (A-J), fama_french_12-ish group

Only CIKs in the universe are fetched (about 11k at <= 8 requests a second).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

URL = "https://data.sec.gov/submissions/CIK{cik}.json"
MIN_INTERVAL = 0.13

# SIC division by 2-digit major group (SEC/OSHA scheme)
DIVISIONS = [
    ("A", range(1, 10)), ("B", range(10, 15)), ("C", range(15, 18)), ("D", range(20, 40)),
    ("E", range(40, 50)), ("F", range(50, 52)), ("G", range(52, 60)), ("H", range(60, 68)),
    ("I", range(70, 90)), ("J", range(91, 100)),
]
SCHEMA = pa.schema([("cik", pa.string()), ("sic", pa.string()), ("sic_description", pa.string()),
                    ("division", pa.string()), ("group2", pa.string())])


def division(sic: str | None) -> str | None:
    if not sic or not str(sic).isdigit():
        return None
    major = int(str(sic).zfill(4)[:2])
    return next((d for d, rng in DIVISIONS if major in rng), None)


def user_agent(env_path: Path | None = None) -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    path = env_path or Path(__file__).resolve().parents[1] / ".env"
    if not ua and path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "SEC_USER_AGENT" and value.strip():
                ua = value.strip().strip("'\"")
    if not ua:
        raise SystemExit("SEC_USER_AGENT is not set. The SEC requires a User-Agent that "
                         "identifies you, e.g. SEC_USER_AGENT=\"Your Name you@example.com\" in .env")
    return ua


def fetch(cik: str, ua: str, opener=None) -> dict | None:
    url = URL.format(cik=cik.zfill(10))
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    for attempt in range(5):
        try:
            with (opener or urllib.request.urlopen)(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 403, 500, 502, 503):
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    return None


def sic_row(cik: str, data: dict | None) -> dict:
    sic = str((data or {}).get("sic") or "") or None
    div = division(sic)
    return {"cik": cik, "sic": sic, "sic_description": (data or {}).get("sicDescription"),
            "division": div, "group2": sic[:2] if sic else None}


def pull(ciks: list[str], out: Path, ua: str, *, fetcher=fetch, sleep=time.sleep, log=print) -> int:
    have = {r["cik"] for r in pq.read_table(out).to_pylist()} if out.exists() else set()
    rows = pq.read_table(out).to_pylist() if out.exists() else []
    todo = [c for c in ciks if c not in have]
    for n, cik in enumerate(todo, 1):
        rows.append(sic_row(cik, fetcher(cik, ua)))
        sleep(MIN_INTERVAL)
        if n % 500 == 0:
            log(f"  {n:,}/{len(todo):,}")
            _write(rows, out)
    _write(rows, out)
    return len(todo)


def _write(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp)
    os.replace(tmp, out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--universe", default="data/features/universe.parquet")
    p.add_argument("--out", default="data/ref/sic.parquet")
    a = p.parse_args(argv)
    ciks = sorted({r for r in pq.read_table(a.universe, columns=["cik"]).column("cik").to_pylist()})
    n = pull(ciks, Path(a.out), user_agent())
    print(f"fetched SIC for {n:,} companies -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
