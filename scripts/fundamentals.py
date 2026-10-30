"""Point-in-time financial health from SEC XBRL company facts, and a fixed resilience gate.

    python scripts/fundamentals.py pull --ciks-from FILE.parquet [FILE ...]   (needs SEC_USER_AGENT)
    python scripts/fundamentals.py show --cik 1745020 --asof 2024-04-01

User decision 2026-10-30 ("do all your suggestions"): the darlings run up in their first year
and about a quarter then lose most of their value for good; Reddit cannot tell Rocket Lab from
FuboTV, the balance sheet can. ``pull`` saves each company's ``companyfacts`` JSON from
data.sec.gov (free; the SEC asks for a User-Agent naming you, ``SEC_USER_AGENT``, and at most
ten requests a second) to ``data/ref/companyfacts/<CIK10>.json.gz``.

``metrics(facts, asof)`` reads only facts *filed* on or before ``asof``, so it is point-in-time:

* cash: cash and equivalents plus short-term investments, the latest balance sheet filed;
* debt: long-term debt (current and non-current), notes and convertibles; none reported = 0;
* operating cash flow and revenue over the trailing twelve months: the last annual figure plus
  this year's year-to-date minus last year's same period, when the filings allow, else the last
  annual figure;
* revenue growth: that trailing revenue against the same measure as known a year earlier;
* dilution: shares outstanding now against a year before, adjusted for the stock splits in
  between (a 4:1 split is not dilution);
* runway: years of cash at the current operating burn (infinite when cash flow is positive).

``gate(m)`` is fixed before looking at any outcome and is not to be tuned on the darlings: a
company passes with (1) positive operating cash flow or at least two years of runway, (2) at
most 20% more shares than a year ago, (3) growing trailing revenue, and (4) debt covered by
cash plus three years of positive operating cash flow. Foreign filers (20-F, IFRS), shells and
companies without these facts are ``unknown``, reported apart.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MIN_INTERVAL = 0.15
DIR = Path("data/ref/companyfacts")

CASH = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks", "Cash")
SHORT_TERM = ("ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent")
DEBT_TOTAL = ("LongTermDebt",)
DEBT_PARTS = ("LongTermDebtNoncurrent", "LongTermDebtCurrent", "ConvertibleNotesPayable",
              "ConvertibleLongTermNotesPayable", "LongTermNotesPayable", "DebtCurrent")
OCF = ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
           "RevenueFromContractWithCustomerIncludingAssessedTax")
SHARES = (("dei", "EntityCommonStockSharesOutstanding"), ("us-gaap", "CommonStockSharesOutstanding"))

# the gate, fixed 2026-10-30 before any outcome was looked at
MIN_RUNWAY_YEARS = 2.0
MAX_DILUTION = 0.20
DEBT_OCF_YEARS = 3.0


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _facts(facts: dict, concept: str, ns: str = "us-gaap", unit: str = "USD", asof: str | None = None) -> list[dict]:
    units = (((facts.get("facts") or {}).get(ns) or {}).get(concept) or {}).get("units") or {}
    rows = units.get(unit) or []
    return [r for r in rows if r.get("val") is not None and r.get("end") and (asof is None or r.get("filed", "9") <= asof)]


def instant(facts: dict, concepts, asof: str, ns: str = "us-gaap", unit: str = "USD",
            before: str | None = None, sum_classes: bool = False) -> tuple[float, str] | None:
    """The latest balance-sheet value filed by ``asof`` (and dated on or before ``before``), across
    the concepts (a company switches concept names over the years; the most recent date wins,
    the earlier concept on a tie). ``sum_classes`` adds the facts one filing reports for the
    same date (share classes on the cover page: Pinterest's A and B)."""
    best = None
    for rank, c in enumerate(concepts):
        groups: dict[tuple, list] = {}
        for r in _facts(facts, c, ns, unit, asof):
            if r.get("start") or (before and r["end"] > before):
                continue
            groups.setdefault((r["end"], r.get("filed", ""), r.get("accn", "")), []).append(float(r["val"]))
        for (end, filed, _), vals in groups.items():
            val = sum(vals) if sum_classes else vals[-1]
            key = (end, -rank, filed)
            if best is None or key > best[0]:
                best = (key, val)
    return (best[1], best[0][0]) if best else None


def ttm(facts: dict, concepts, asof: str) -> tuple[float, str] | None:
    """Trailing twelve months of a flow (cash flow, revenue) as known on ``asof``: per concept,
    then the concept with the most recent period (NVIDIA moved from Revenues to
    RevenueFromContractWithCustomer...; taking the first concept with any data read a stale
    year, found 2026-10-30)."""
    found = [(r[1], -i, r[0]) for i, c in enumerate(concepts) if (r := _ttm_one(facts, c, asof)) is not None]
    if not found:
        return None
    end, _, val = max(found)
    return val, end


def _ttm_one(facts: dict, c: str, asof: str) -> tuple[float, str] | None:
    rows = [r for r in _facts(facts, c, asof=asof) if r.get("start")]
    if not rows:
        return None
    by_span: dict[tuple[str, str], tuple[str, float]] = {}
    for r in rows:   # the latest filing of each (start, end) span wins (restatements)
        k = (r["start"], r["end"])
        if k not in by_span or r.get("filed", "") > by_span[k][0]:
            by_span[k] = (r.get("filed", ""), float(r["val"]))
    spans = {k: v for k, (_, v) in by_span.items()}
    dur = {k: (_d(k[1]) - _d(k[0])).days for k in spans}
    annual = sorted((k for k in spans if 350 <= dur[k] <= 380), key=lambda k: k[1])
    latest_end = max(k[1] for k in spans)
    if annual and annual[-1][1] == latest_end:
        return spans[annual[-1]], latest_end
    ytd = [k for k in spans if k[1] == latest_end and dur[k] < 350]
    for k in sorted(ytd, key=lambda k: -dur[k]):
        prior = [q for q in spans if abs((_d(k[1]) - _d(q[1])).days - 365) <= 12 and abs(dur[q] - dur[k]) <= 12]
        fy = [y for y in annual if _d(k[0]) - timedelta(days=12) <= _d(y[1]) <= _d(k[1])]
        if prior and fy:
            return spans[fy[-1]] + spans[k] - spans[prior[0]], latest_end
    if annual:
        return spans[annual[-1]], annual[-1][1]
    return None


STALE_DAYS = 460   # a trailing figure whose period ended this long before the date is too old to use


def split_ratio(splits: list[tuple[str, float]], after: str, upto: str) -> float:
    r = 1.0
    for d, ratio in splits or []:
        if after < d <= upto and ratio > 0:
            r *= ratio
    return r


def metrics(facts: dict | None, asof: str, splits: list[tuple[str, float]] | None = None) -> dict:
    """Point-in-time health as known on ``asof`` (YYYY-MM-DD)."""
    if not facts:
        return {"known": False, "why": "no SEC company facts"}
    cash = instant(facts, CASH, asof)
    sti = instant(facts, SHORT_TERM, asof)
    total = instant(facts, DEBT_TOTAL, asof)
    if total is not None:
        debt = total[0]
    else:
        parts = [instant(facts, (c,), asof) for c in DEBT_PARTS]
        debt = sum(p[0] for p in parts if p is not None)
    ocf = ttm(facts, OCF, asof)
    rev = ttm(facts, REVENUE, asof)
    a = _d(asof)
    rev_prev = ttm(facts, REVENUE, (a - timedelta(days=365)).isoformat())
    sh = None
    for ns, c in SHARES:
        sh = instant(facts, (c,), asof, ns=ns, unit="shares", sum_classes=ns == "dei")
        if sh is not None:
            break
    sh_prev = None
    if sh is not None:
        cut = (_d(sh[1]) - timedelta(days=330)).isoformat()
        for ns, c in SHARES:
            sh_prev = instant(facts, (c,), asof, ns=ns, unit="shares", before=cut, sum_classes=ns == "dei")
            if sh_prev is not None:
                break
    m = {"known": cash is not None and ocf is not None, "asof": asof,
         "cash": (cash[0] if cash else 0.0) + (sti[0] if sti else 0.0) if cash else None,
         "cash_date": cash[1] if cash else None, "debt": debt,
         "ocf_ttm": ocf[0] if ocf else None, "revenue_ttm": rev[0] if rev else None,
         "revenue_ttm_prev": rev_prev[0] if rev_prev else None,
         "revenue_end": rev[1] if rev else None, "revenue_prev_end": rev_prev[1] if rev_prev else None,
         "shares": sh[0] if sh else None, "shares_prev": sh_prev[0] if sh_prev else None}
    if not m["known"]:
        m["why"] = "no cash or operating cash flow filed by then"
        return m
    m["runway_years"] = math.inf if m["ocf_ttm"] >= 0 else m["cash"] / -m["ocf_ttm"]
    fresh = rev is not None and rev_prev is not None and (a - _d(rev[1])).days <= STALE_DAYS and rev[1] != rev_prev[1]
    m["revenue_growth"] = (m["revenue_ttm"] / m["revenue_ttm_prev"] - 1
                           if fresh and m["revenue_ttm_prev"] else None)
    if sh and sh_prev and sh_prev[0] > 0:
        m["dilution"] = sh[0] / (sh_prev[0] * split_ratio(splits, sh_prev[1], sh[1])) - 1
    else:
        m["dilution"] = None
    return m


def gate(m: dict) -> tuple[str, list[str]]:
    """('pass' | 'fail' | 'unknown', reasons)."""
    if not m.get("known"):
        return "unknown", [m.get("why") or "unknown"]
    why = []
    if m["runway_years"] < MIN_RUNWAY_YEARS:
        why.append(f"runway {m['runway_years']:.1f}y")
    if m.get("dilution") is not None and m["dilution"] > MAX_DILUTION:
        why.append(f"dilution {m['dilution']:+.0%}")
    if m.get("revenue_growth") is None:
        if not m.get("revenue_ttm"):
            why.append("no revenue")
    elif m["revenue_growth"] <= 0:
        why.append(f"revenue {m['revenue_growth']:+.0%}")
    cover = m["cash"] + DEBT_OCF_YEARS * max(m["ocf_ttm"], 0.0)
    if m["debt"] > cover:
        why.append(f"debt ${m['debt'] / 1e6:,.0f}M > cash + 3y cash flow ${cover / 1e6:,.0f}M")
    return ("fail" if why else "pass"), why


def describe(m: dict) -> str:
    if not m.get("known"):
        return m.get("why") or "unknown"
    rw = "cash-flow positive" if math.isinf(m["runway_years"]) else f"runway {m['runway_years']:.1f}y"
    parts = [f"cash ${m['cash'] / 1e6:,.0f}M", f"debt ${m['debt'] / 1e6:,.0f}M", rw]
    if m.get("revenue_growth") is not None:
        parts.append(f"revenue {m['revenue_growth']:+.0%}")
    if m.get("dilution") is not None:
        parts.append(f"shares {m['dilution']:+.0%}")
    return ", ".join(parts)


class Facts:
    """Lazy reader of the saved company facts."""

    def __init__(self, root: Path = DIR):
        self.root, self._cache = Path(root), {}

    def get(self, cik: str) -> dict | None:
        cik = str(cik).zfill(10)
        if cik not in self._cache:
            p = self.root / f"{cik}.json.gz"
            self._cache[cik] = json.loads(gzip.decompress(p.read_bytes())) if p.exists() else None
        return self._cache[cik]


def fetch(cik: str, ua: str, opener=None) -> bytes | None:
    req = urllib.request.Request(URL.format(cik=str(cik).zfill(10)),
                                 headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    for attempt in range(5):
        try:
            with (opener or urllib.request.urlopen)(req, timeout=60) as r:
                return r.read()
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


def pull(ciks: list[str], root: Path, ua: str, *, fetcher=fetch, sleep=time.sleep, log=print) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    stats = {"cached": 0, "saved": 0, "missing": 0}
    for n, cik in enumerate(sorted(set(str(c).zfill(10) for c in ciks)), 1):
        p = root / f"{cik}.json.gz"
        miss = root / f"{cik}.missing"
        if p.exists() or miss.exists():
            stats["cached"] += 1
            continue
        data = fetcher(cik, ua)
        sleep(MIN_INTERVAL)
        if data is None:
            miss.write_text("no companyfacts (404): a foreign filer, a fund or a shell\n")
            stats["missing"] += 1
        else:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(gzip.compress(data))
            os.replace(tmp, p)
            stats["saved"] += 1
        if n % 100 == 0:
            log(f"  {n:,}/{len(set(ciks)):,} {stats}")
    log(f"[fundamentals] {stats}")
    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = p.add_subparsers(dest="cmd", required=True)
    a1 = sp.add_parser("pull")
    a1.add_argument("--ciks-from", nargs="+", required=True, help="parquet files with a cik column")
    a1.add_argument("--out", default=str(DIR))
    a2 = sp.add_parser("show")
    a2.add_argument("--cik", required=True)
    a2.add_argument("--asof", required=True, help="one date or several, comma-separated")
    a2.add_argument("--dir", default=str(DIR))
    a = p.parse_args(argv)
    if a.cmd == "pull":
        import pyarrow.parquet as pq
        from edgar import user_agent
        ciks = set()
        for f in a.ciks_from:
            if Path(f).exists():
                ciks |= {str(c) for c in pq.read_table(f, columns=["cik"]).column("cik").to_pylist() if c}
        print(f"[fundamentals] {len(ciks):,} companies", flush=True)
        pull(sorted(ciks), Path(a.out), user_agent())
        return 0
    facts = Facts(Path(a.dir)).get(a.cik)
    for asof in a.asof.split(","):
        m = metrics(facts, asof)
        print(json.dumps(m, default=str, indent=1))
        print(gate(m), describe(m))
    for c in REVENUE:   # what the filings hold, for checking the trailing-twelve-months arithmetic
        rows = sorted(_facts(facts or {}, c, asof=a.asof.split(",")[-1]), key=lambda r: (r["end"], r.get("start", "")))
        print(c, [(r.get("start"), r["end"], r["val"], r.get("filed"), r.get("form")) for r in rows if r["end"] >= "2018-06"][:40])
    return 0


if __name__ == "__main__":
    sys.exit(main())
