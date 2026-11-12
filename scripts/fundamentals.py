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
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MIN_INTERVAL = 0.15
DIR = Path("data/ref/companyfacts")
# Every company's facts in one file, refreshed nightly by the SEC (1.4 GB on 2026-11-12, checked
# with sec_bulk.py); ``Facts`` falls back to it for companies without a per-company file.
BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
BULK = Path("data/ref/companyfacts.zip")

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
            val = sum(vals) if sum_classes else max(vals) if unit == "shares" else vals[-1]
            key = (end, -rank, filed)
            if best is None or key > best[0]:
                best = (key, val)
    return (best[1], best[0][0]) if best else None


def ttm(facts: dict, concepts, asof: str, ns: str = "us-gaap", unit: str = "USD") -> tuple[float, str] | None:
    """Trailing twelve months of a flow (cash flow, revenue) as known on ``asof``: the last
    annual figure plus this year's year-to-date minus last year's same period, else the last
    annual figure. The concepts are pooled first (an earlier concept wins an identical span):
    NVIDIA files quarters under ``Revenues`` and years under
    ``RevenueFromContractWithCustomerExcludingAssessedTax``, so one concept alone either has no
    year or no quarter (its 2020 revenue read -7% when it was +28%, found 2026-10-30)."""
    spans: dict[tuple[str, str], tuple[int, str, float]] = {}
    for rank, c in enumerate(concepts):
        for r in _facts(facts, c, ns, unit, asof):
            if not r.get("start"):
                continue
            k = (r["start"], r["end"])
            cand = (-rank, r.get("filed", ""), float(r["val"]))   # earlier concept, then latest filing
            if k not in spans or cand[:2] > spans[k][:2]:
                spans[k] = cand
    if not spans:
        return None
    val = {k: v[2] for k, v in spans.items()}
    dur = {k: (_d(k[1]) - _d(k[0])).days for k in val}
    annual = sorted((k for k in val if 350 <= dur[k] <= 380), key=lambda k: k[1])
    latest_end = max(k[1] for k in val)
    if annual and annual[-1][1] == latest_end:
        return val[annual[-1]], latest_end
    ytd = [k for k in val if k[1] == latest_end and dur[k] < 350]
    for k in sorted(ytd, key=lambda k: -dur[k]):
        prior = [q for q in val if abs((_d(k[1]) - _d(q[1])).days - 365) <= 12 and abs(dur[q] - dur[k]) <= 12]
        fy = [y for y in annual if _d(k[0]) - timedelta(days=12) <= _d(y[1]) <= _d(k[1])]
        if prior and fy:
            return val[fy[-1]] + val[k] - val[prior[0]], latest_end
    if annual:
        return val[annual[-1]], annual[-1][1]
    return None


STALE_DAYS = 460   # a trailing figure whose period ended this long before the date is too old to use


SHARES_MAX_AGE = 400   # days: an older share count is not "shares outstanding now"


def shares_at(facts: dict, asof: str, before: str | None = None) -> tuple[float, str] | None:
    """Shares outstanding as known on ``asof`` (dated on or before ``before`` when given): the
    cover-page count (classes summed), else the income statement's weighted average of basic
    shares, else the balance-sheet count. The bulk file leaves out facts reported by share
    class, so for Lyft and Pinterest the cover page is missing or partial for years; the weighted
    average is reported for all classes together (found 2026-11-12)."""
    ref = _d(before or asof)
    cands = [instant(facts, ("EntityCommonStockSharesOutstanding",), asof, "dei", "shares", before=before,
                     sum_classes=True)]
    rows = [r for r in _facts(facts, "WeightedAverageNumberOfSharesOutstandingBasic", "us-gaap", "shares", asof)
            if r.get("start") and (before is None or r["end"] <= before) and r["val"] > 0]
    if rows:
        best = max(rows, key=lambda r: (r["end"], r["start"], r.get("filed", "")))
        cands.append((float(best["val"]), best["end"]))
    cands.append(instant(facts, ("CommonStockSharesOutstanding",), asof, "us-gaap", "shares", before=before))
    for c in cands:
        if c and c[0] > 0 and (ref - _d(c[1])).days <= SHARES_MAX_AGE:
            return c
    return None


def split_ratio(splits: list[tuple[str, float]], after: str, upto: str) -> float:
    r = 1.0
    for d, ratio in splits or []:
        if after < d <= upto and ratio > 0:
            r *= ratio
    return r


# IFRS concept names for foreign filers (20-F / 40-F), from what the bulk file holds for SAP, TSM and
# NVO (inspected 2026-11-12): total `Borrowings` where filed, else its parts. ASML files US GAAP in
# euros, so the basis and the currency are picked from the filings, never assumed.
IFRS_SHORT_TERM = ("CurrentFinancialAssetsAtAmortisedCost", "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
                   "CurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome", "ShorttermInvestments")
IFRS_DEBT_PARTS = ("NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings",
                   "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "CurrentPortionOfLongtermBorrowings",
                   "ShorttermBorrowings")

GAAP = {"ns": "us-gaap", "cash": CASH, "short_term": SHORT_TERM, "debt_total": DEBT_TOTAL,
        "debt_parts": DEBT_PARTS, "ocf": OCF, "revenue": REVENUE,
        "net_income": ("NetIncomeLoss", "ProfitLoss"), "op_income": ("OperatingIncomeLoss",),
        "gross_profit": ("GrossProfit",), "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
        "assets": ("Assets",), "liabilities": ("Liabilities",),
        "equity": ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")}
IFRS = {"ns": "ifrs-full", "cash": ("CashAndCashEquivalents",), "short_term": IFRS_SHORT_TERM,
        "debt_total": ("Borrowings",), "debt_parts": IFRS_DEBT_PARTS, "ocf": ("CashFlowsFromUsedInOperatingActivities",),
        "revenue": ("Revenue", "RevenueFromContractsWithCustomers"),
        "net_income": ("ProfitLoss", "ProfitLossAttributableToOwnersOfParent"),
        "op_income": ("ProfitLossFromOperatingActivities",), "gross_profit": ("GrossProfit",),
        "capex": ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",),
        "assets": ("Assets",), "liabilities": ("Liabilities",),
        "equity": ("Equity", "EquityAttributableToOwnersOfParent")}
EXTRA_FLOWS = ("net_income", "op_income", "gross_profit", "capex")
EXTRA_STOCKS = ("assets", "liabilities", "equity")


def profile_for(facts: dict | None) -> tuple[dict, str]:
    """The basis (US GAAP or IFRS) and currency the company reports its core figures in: the pair
    with the most revenue, cash and operating-cash-flow facts. ASML files US GAAP in euros, SAP
    IFRS in euros, TSM IFRS in Taiwan dollars (plus a few convenience-translation dollar facts)."""
    f = (facts or {}).get("facts") or {}
    score: dict[tuple[int, str], list] = {}
    for i, pr in enumerate((GAAP, IFRS)):
        block = f.get(pr["ns"]) or {}
        for c in (*pr["revenue"], *pr["cash"], *pr["ocf"]):
            for u, rows in ((block.get(c) or {}).get("units") or {}).items():
                if "/" not in u and u not in ("shares", "pure") and rows:
                    sc = score.setdefault((i, u), ["", 0])
                    sc[0] = max(sc[0], max(r.get("filed", "") for r in rows))[:4]   # the latest year it filed in
                    sc[1] += len(rows)
    if not score:
        return GAAP, "USD"
    # the most recent filing year first (Nebius moved from roubles to dollars), then the most facts
    # (TSM files a few convenience dollar figures next to its Taiwan-dollar statements)
    i, u = max(score, key=lambda k: tuple(score[k]))
    return (GAAP, IFRS)[i], u


def metrics(facts: dict | None, asof: str, splits: list[tuple[str, float]] | None = None,
            profile: dict | None = None, unit: str = "USD") -> dict:
    """Point-in-time health as known on ``asof`` (YYYY-MM-DD), in ``unit`` (the filer's currency)."""
    if not facts:
        return {"known": False, "why": "no SEC company facts"}
    pr = profile or GAAP
    ns = pr["ns"]
    cash = instant(facts, pr["cash"], asof, ns, unit)
    sti = instant(facts, pr["short_term"], asof, ns, unit)
    total = instant(facts, pr["debt_total"], asof, ns, unit) if pr["debt_total"] else None
    if total is not None:
        debt = total[0]
    else:
        parts = [instant(facts, (c,), asof, ns, unit) for c in pr["debt_parts"]]
        debt = sum(p[0] for p in parts if p is not None)
    ocf = ttm(facts, pr["ocf"], asof, ns, unit)
    rev = ttm(facts, pr["revenue"], asof, ns, unit)
    a = _d(asof)
    rev_prev = ttm(facts, pr["revenue"], (a - timedelta(days=365)).isoformat(), ns, unit)
    sh = shares_at(facts, asof)
    sh_prev = shares_at(facts, asof, before=(_d(sh[1]) - timedelta(days=330)).isoformat()) if sh else None
    stale = [k for k, v in (("balance sheet", cash), ("cash flow", ocf))
             if v is not None and (a - _d(v[1])).days > STALE_DAYS]
    m = {"known": cash is not None and ocf is not None and not stale, "asof": asof,
         "cash": (cash[0] if cash else 0.0) + (sti[0] if sti else 0.0) if cash else None,
         "cash_date": cash[1] if cash else None, "debt": debt,
         "ocf_ttm": ocf[0] if ocf else None, "revenue_ttm": rev[0] if rev else None,
         "revenue_ttm_prev": rev_prev[0] if rev_prev else None,
         "revenue_end": rev[1] if rev else None, "revenue_prev_end": rev_prev[1] if rev_prev else None,
         "shares": sh[0] if sh else None, "shares_prev": sh_prev[0] if sh_prev else None,
         "shares_date": sh[1] if sh else None, "currency": unit, "basis": ns}
    for k in EXTRA_FLOWS:
        v = ttm(facts, pr[k], asof, ns, unit)
        m[f"{k}_ttm"] = v[0] if v and (a - _d(v[1])).days <= STALE_DAYS else None
    for k in EXTRA_STOCKS:
        v = instant(facts, pr[k], asof, ns, unit)
        m[k] = v[0] if v else None
    if not m["known"]:
        m["why"] = (f"no {' or '.join(stale)} filed in the {STALE_DAYS} days before" if stale
                    else "no cash or operating cash flow filed by then")
        return m
    m["runway_years"] = math.inf if m["ocf_ttm"] >= 0 else m["cash"] / -m["ocf_ttm"]
    fresh = rev is not None and rev_prev is not None and (a - _d(rev[1])).days <= STALE_DAYS and rev[1] != rev_prev[1]
    m["revenue_growth"] = (m["revenue_ttm"] / m["revenue_ttm_prev"] - 1
                           if fresh and m["revenue_ttm_prev"] else None)
    m["dilution"] = None
    if sh and sh_prev and sh_prev[0] > 0:
        d = sh[0] / (sh_prev[0] * split_ratio(splits, sh_prev[1], sh[1])) - 1
        # a share count that fell 80%+ in a year is a missing share class or an unadjusted split,
        # not a buyback (LYFT and PINS read -100% on 2026-10-30)
        m["dilution"] = d if d > -0.8 else None
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
    cur = "$" if m.get("currency", "USD") == "USD" else f"{m['currency']} "
    parts = [f"cash {cur}{m['cash'] / 1e6:,.0f}M", f"debt {cur}{m['debt'] / 1e6:,.0f}M", rw]
    if m.get("revenue_growth") is not None:
        parts.append(f"revenue {m['revenue_growth']:+.0%}")
    if m.get("dilution") is not None:
        parts.append(f"shares {m['dilution']:+.0%}")
    return ", ".join(parts)


class Facts:
    """Lazy reader of the saved company facts: a per-company file first, then the bulk zip."""

    def __init__(self, root: Path = DIR, bulk: Path | None = BULK, cache: bool = True):
        self.root, self._cache, self.keep = Path(root), {}, cache
        self._zip = zipfile.ZipFile(bulk) if bulk and Path(bulk).exists() else None
        self._names = None

    def member(self, cik: str) -> str | None:
        if self._zip is None:
            return None
        if self._names is None:
            self._names = {Path(n).stem.upper().removeprefix("CIK"): n for n in self._zip.namelist()}
        return self._names.get(str(cik).zfill(10))

    def get(self, cik: str) -> dict | None:
        cik = str(cik).zfill(10)
        if cik in self._cache:
            return self._cache[cik]
        p = self.root / f"{cik}.json.gz"
        if p.exists():
            out = json.loads(gzip.decompress(p.read_bytes()))
        else:
            name = self.member(cik)
            out = json.loads(self._zip.read(name)) if name else None
        if self.keep:
            self._cache[cik] = out
        return out


def bulk_pull(path: Path, ua: str, opener=None, log=print, chunk: int = 1 << 22) -> int:
    """Download the nightly companyfacts.zip to ``path``, resuming a partial ``.part`` file and
    checking the final size against the server's. Returns the size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path.parent).free
    log(f"[fundamentals] {free / 1e9:,.1f} GB free on the volume")
    if free < 3e9:
        raise SystemExit("less than 3 GB free: not downloading the 1.4 GB companyfacts.zip")
    part = path.with_name(path.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": ua, "Accept-Encoding": "identity"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(BULK_URL, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=120) as r:
        total = (r.headers.get("Content-Range") or "").rpartition("/")[2] or r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        if have and r.status != 206:   # the server ignored the range: start over
            have = 0
        if have and not (r.headers.get("Content-Range") or "").startswith(f"bytes {have}-"):
            have = 0
        mode = "ab" if have else "wb"
        if have and r.headers.get("Content-Length"):
            total = have + int(r.headers["Content-Length"])
        done, last = have, time.time()
        with open(part, mode) as f:
            while True:
                b = r.read(chunk)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if time.time() - last > 30:
                    log(f"  {done / 1e6:,.0f} MB" + (f" of {total / 1e6:,.0f} MB" if total else ""))
                    last = time.time()
    size = part.stat().st_size
    if total and size != total:
        raise SystemExit(f"companyfacts.zip is {size:,} bytes, expected {total:,}: run again to resume")
    with zipfile.ZipFile(part) as z:   # a truncated zip has no readable directory
        n = len(z.namelist())
    os.replace(part, path)
    log(f"[fundamentals] companyfacts.zip: {size / 1e6:,.0f} MB, {n:,} companies -> {path}")
    return size


def inspect_bulk(facts: "Facts", universe: Path, tickers: list[str], log=print) -> None:
    """What the bulk file holds for our universe, and the raw facts behind a few known problems."""
    import duckdb
    con = duckdb.connect()
    rows = con.execute(f"""select cik, min(month) first, max(month) last, arg_max(ticker, month) ticker
                            from read_parquet('{universe}') where eligible group by cik""").fetchall()
    names = list(facts._zip.namelist()) if facts._zip else []
    log(f"[inspect] {len(names):,} files in the zip, e.g. {names[:3]}")
    by_year: dict[str, list[int]] = {}
    for cik, first, last, _ in rows:
        by_year.setdefault(first[:4], [0, 0])
        by_year[first[:4]][0] += 1
        by_year[first[:4]][1] += facts.member(cik) is not None
    log(f"[inspect] universe companies ever eligible: {len(rows):,}; in the zip by first eligible year: "
        + ", ".join(f"{y} {h}/{n}" for y, (n, h) in sorted(by_year.items())))
    cik_of = {t: c for c, _, _, t in rows}
    pat = ("Revenue", "Cash", "Borrow", "Debt", "ProfitLoss", "OperatingActivities", "Equity", "Assets",
           "Liabilit", "GrossProfit", "Shares", "PropertyPlant")
    for t in tickers:
        cik = cik_of.get(t)
        f = facts.get(cik) if cik else None
        if not f:
            log(f"\n-- {t}: cik {cik}, no facts")
            continue
        ns = {k: len(v) for k, v in (f.get("facts") or {}).items()}
        log(f"\n-- {t} cik {cik} {f.get('entityName')}: concepts per namespace {ns}")
        for k, v in sorted(((f.get("facts") or {}).get("dei") or {}).items()):
            if "Shares" in k:
                for unit, rs in v["units"].items():
                    last8 = sorted(rs, key=lambda r: (r.get("end", ""), r.get("filed", "")))[-8:]
                    log(f"   dei {k} [{unit}]: " + "; ".join(f"{r.get('end')} filed {r.get('filed')} {r.get('form')} "
                                                        f"{r.get('accn')} {r['val']:,.0f}" for r in last8))
        if "ifrs-full" in (f.get("facts") or {}):
            hits = []
            for k, v in sorted(f["facts"]["ifrs-full"].items()):
                if any(p in k for p in pat):
                    units = {u: max((r.get("end", "") for r in rs), default="") for u, rs in v["units"].items()}
                    hits.append(f"{k} {units}")
            log(f"   ifrs-full ({len(hits)} matching): " + " | ".join(hits[:80]))


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


# ------------------------------------------------------------------ the monthly panel

PANEL = Path("data/features/fundamentals.parquet")
PANEL_SCHEMA = pa.schema([
    ("month", pa.string()), ("cik", pa.string()), ("basis", pa.string()), ("currency", pa.string()),
    ("health", pa.string()), ("health_why", pa.string()), ("known", pa.bool_()),
    ("cash", pa.float64()), ("debt", pa.float64()), ("ocf_ttm", pa.float64()), ("revenue_ttm", pa.float64()),
    ("revenue_growth", pa.float64()), ("gross_profit_ttm", pa.float64()), ("op_income_ttm", pa.float64()),
    ("net_income_ttm", pa.float64()), ("capex_ttm", pa.float64()), ("assets", pa.float64()),
    ("liabilities", pa.float64()), ("equity", pa.float64()), ("shares", pa.float64()),
    ("shares_date", pa.string()), ("dilution", pa.float64()), ("runway_years", pa.float64()),
    ("last_filed", pa.string()), ("mcap", pa.float64())])
FILING_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "40-F/A", "6-K")


def filed_dates(facts: dict | None) -> list[str]:
    """Every date the company filed a financial report, sorted (for staleness)."""
    out = set()
    for ns in ((facts or {}).get("facts") or {}).values():
        for c in ns.values():
            for rows in (c.get("units") or {}).values():
                out.update(r["filed"] for r in rows if r.get("filed") and r.get("form") in FILING_FORMS)
    return sorted(out)


def company_rows(cik: str, rows: list[dict], facts: dict | None, splits_of) -> list[dict]:
    """The panel rows of one company: ``rows`` are its eligible universe months (month, code, price).
    A cohort month's cutoff is 00:00 UTC on its first day, so only facts filed the day before or
    earlier are used."""
    import bisect
    prof, cur = profile_for(facts)
    filed = filed_dates(facts)
    out = []
    for u in rows:
        asof = (_d(u["month"]) - timedelta(days=1)).isoformat()
        splits = splits_of(u.get("code"))
        m = metrics(facts, asof, splits, prof, cur)
        st, why = gate(m)
        i = bisect.bisect_right(filed, asof)
        mcap = None
        if cur == "USD" and m.get("shares") and u.get("price"):
            mcap = m["shares"] * split_ratio(splits, m["shares_date"], asof) * u["price"]
        rw = m.get("runway_years")
        out.append({"month": u["month"], "cik": cik, "basis": m.get("basis"), "currency": m.get("currency"),
                    "health": st, "health_why": "; ".join(why), "known": bool(m.get("known")),
                    **{k: m.get(k) for k in ("cash", "debt", "ocf_ttm", "revenue_ttm", "revenue_growth",
                                             "gross_profit_ttm", "op_income_ttm", "net_income_ttm", "capex_ttm",
                                             "assets", "liabilities", "equity", "shares", "shares_date", "dilution")},
                    "runway_years": None if rw is None else (1e9 if math.isinf(rw) else rw),
                    "last_filed": filed[i - 1] if i else None, "mcap": mcap})
    return out


_W: dict = {}


def _init_worker(bulk: str, root: str, eodhd: str) -> None:
    _W["facts"] = Facts(Path(root), Path(bulk), cache=False)
    _W["eodhd"] = Path(eodhd)
    _W["splits"] = {}


def _splits(code):
    if code not in _W["splits"]:
        from darlings import splits_for
        _W["splits"][code] = splits_for(_W["eodhd"], code)
    return _W["splits"][code]


def _work(item):
    cik, rows = item
    try:
        return company_rows(cik, rows, _W["facts"].get(cik), _splits), None
    except Exception as e:   # one malformed company must not stop 12,000
        return [], f"{cik}: {e!r}"


def build_panel(universe: Path, out: Path, bulk: Path, root: Path, eodhd: Path, workers: int, log=print) -> int:
    import duckdb
    import multiprocessing as mp
    con = duckdb.connect()
    by: dict[str, list[dict]] = {}
    for month, cik, code, price in con.execute(f"""select month, cik, code, price from read_parquet('{universe}')
                                                  where eligible order by cik, month""").fetchall():
        by.setdefault(cik, []).append({"month": month, "code": code, "price": price})
    con.close()   # duckdb runs threads; the pool spawns fresh processes rather than forking them
    log(f"[fundamentals] panel for {len(by):,} companies, {sum(map(len, by.values())):,} eligible company-months, "
        f"{workers} workers")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    n, errors, t0 = 0, [], time.time()
    with pq.ParquetWriter(tmp, PANEL_SCHEMA, compression="zstd") as w, \
            mp.get_context("spawn").Pool(workers, initializer=_init_worker,
                                         initargs=(str(bulk), str(root), str(eodhd))) as pool:
        for k, (rows, err) in enumerate(pool.imap_unordered(_work, sorted(by.items()), chunksize=8), 1):
            if err:
                errors.append(err)
            if rows:
                w.write_table(pa.Table.from_pylist(rows, schema=PANEL_SCHEMA))
                n += len(rows)
            if k % 1000 == 0:
                log(f"  {k:,}/{len(by):,} companies, {n:,} rows, {time.time() - t0:,.0f}s")
    tmp.replace(out)
    log(f"[fundamentals] wrote {n:,} rows to {out}; {len(errors)} companies failed" +
        (": " + "; ".join(errors[:5]) if errors else ""))
    return n


def panel_summary(path: Path, log=print) -> None:
    """Coverage and sanity of the panel, by year."""
    import duckdb
    con = duckdb.connect()
    log("year  rows  health pass/fail/unknown  basis ifrs  revenue known  mcap known  median mcap $M")
    for r in con.execute(f"""select substr(month, 1, 4) y, count(*),
                                    avg((health = 'pass')::int), avg((health = 'fail')::int), avg((health = 'unknown')::int),
                                    avg((basis = 'ifrs-full')::int), avg((revenue_ttm is not null)::int),
                                    avg((mcap is not null)::int), median(mcap) / 1e6
                             from read_parquet('{path}') group by 1 order by 1""").fetchall():
        log(f"{r[0]}  {r[1]:6,d}  {r[2]:.0%} / {r[3]:.0%} / {r[4]:.0%}  {r[5]:.1%}  {r[6]:.0%}  {r[7]:.0%}  {r[8] or 0:,.0f}")
    log("fail reasons (share of failing rows): " + ", ".join(
        f"{k} {v:.0%}" for k, v in con.execute(f"""
            with f as (select unnest(string_split(health_why, '; ')) w from read_parquet('{path}') where health = 'fail')
            select split_part(w, ' ', 1) k, count(*) / (select count(*) from read_parquet('{path}') where health = 'fail')
            from f group by 1 order by 2 desc limit 6""").fetchall()))


SPOT = [("LYFT", "2025-03-01"), ("PINS", "2026-03-01"), ("ASML", "2026-01-01"), ("NVDA", "2024-12-01"),
        ("AAPL", "2020-10-01"), ("SAP", "2026-02-01"), ("TSM", "2025-01-01"), ("JPM", "2025-01-01"),
        ("RKLB", "2024-04-01"), ("GME", "2021-01-01"), ("NBIS", "2026-05-01"), ("JD", "2025-10-01")]


def spot_check(panel: Path, universe: Path, log=print) -> None:
    """A few known companies, to read the numbers against what is public."""
    import duckdb
    con = duckdb.connect()
    for t, m in SPOT:
        r = con.execute(f"""select u.ticker, f.* exclude (month, cik) from read_parquet('{universe}') u
                            join read_parquet('{panel}') f using (month, cik)
                            where u.month = ? and u.ticker = ?""", [m, t]).fetchone()
        if not r:
            log(f"  {t} {m}: not in the panel")
            continue
        cols = [d[0] for d in con.description]
        d = dict(zip(cols, r))
        log(f"  {t} {m}: {d['basis']} {d['currency']} {d['health']} ({d['health_why'] or 'ok'}); "
            f"mcap {_mm(d, 'mcap')}, revenue ttm {_mm(d, 'revenue_ttm')} ({_pc(d, 'revenue_growth')}), "
            f"net income {_mm(d, 'net_income_ttm')}, ocf {_mm(d, 'ocf_ttm')}, cash {_mm(d, 'cash')}, "
            f"debt {_mm(d, 'debt')}, shares {_mm(d, 'shares')} on {d['shares_date']}, "
            f"dilution {_pc(d, 'dilution')}, last filed {d['last_filed']}")


def _mm(d: dict, k: str) -> str:
    v = d.get(k)
    return "n/a" if v is None else f"{v / 1e6:,.0f}M"


def _pc(d: dict, k: str) -> str:
    v = d.get(k)
    return "n/a" if v is None else f"{v:+.1%}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = p.add_subparsers(dest="cmd", required=True)
    a1 = sp.add_parser("pull")
    a1.add_argument("--ciks-from", nargs="+", required=True, help="parquet files with a cik column")
    a1.add_argument("--out", default=str(DIR))
    b1 = sp.add_parser("bulk-pull", help="download the nightly companyfacts.zip (resumable)")
    b1.add_argument("--out", default=str(BULK))
    b1.add_argument("--inspect", nargs="*", default=None, metavar="TICKER",
                    help="then print what the zip holds for the universe and these tickers")
    b1.add_argument("--universe", default="data/features/universe.parquet")
    b2 = sp.add_parser("panel", help="the monthly point-in-time table for every eligible company")
    b2.add_argument("--universe", default="data/features/universe.parquet")
    b2.add_argument("--out", default=str(PANEL))
    b2.add_argument("--bulk", default=str(BULK))
    b2.add_argument("--dir", default=str(DIR))
    b2.add_argument("--eodhd", default="data/market/eodhd")
    b2.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2))
    a2 = sp.add_parser("show")
    a2.add_argument("--cik", required=True)
    a2.add_argument("--asof", required=True, help="one date or several, comma-separated")
    a2.add_argument("--dir", default=str(DIR))
    a = p.parse_args(argv)
    if a.cmd == "bulk-pull":
        from edgar import user_agent
        out = Path(a.out)
        if not out.exists():
            bulk_pull(out, user_agent())
        else:
            print(f"[fundamentals] {out} already here ({out.stat().st_size / 1e6:,.0f} MB)", flush=True)
        if a.inspect is not None:
            inspect_bulk(Facts(bulk=out, cache=False), Path(a.universe), a.inspect)
        return 0
    if a.cmd == "panel":
        build_panel(Path(a.universe), Path(a.out), Path(a.bulk), Path(a.dir), Path(a.eodhd), a.workers)
        panel_summary(Path(a.out))
        spot_check(Path(a.out), Path(a.universe))
        return 0
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
