"""Cheap, recall-first relevance filter for Reddit finance posts.

Used while downloading so the full corpus never has to sit on disk. It is
deliberately crude: it only needs to throw away the obvious noise (memes,
megathreads, off-topic chatter). Precise ticker resolution and quality scoring
happen later, on what this keeps.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FILTER_VERSION = "posts-v2"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT = "redditstockscraper research (personal; contact via repo owner)"

# Uppercase words that are also (or look like) tickers but are almost always
# English, slang, or finance jargon on Reddit. Real tickers in here (AI, NOW,
# ON, ET, ...) still match when written as a cashtag ($NOW).
STOPLIST = frozenset(
    """
    A I AM AN AND ANY ARE AS AT BE BIG BY CAN DO FOR GO HAS HE IF IN IS IT ME MY
    NO NOT NOW OF OH OK ON ONE OR OUT SO THE TO UP US WE ALL BEST GOOD NEW OPEN
    PLAY REAL LOVE RUN CASH FUND NEXT LIFE HOME FUN WELL SEE VERY JUST OWN MAIN
    POST CAR GAIN EARN NICE HOLD BUY SELL CALL PUT LONG SHORT MOON BULL BEAR
    PUMP DUMP TECH MEME EDIT TLDR TL DR FOMO HODL YOLO IMO IMHO FYI LOL LMAO WTF
    OMG CEO CFO CTO COO DD EPS PE PEG ROI ROE ROIC FCF EBIT EBITDA TAM SAM ARR
    SAAS API AI ML EV ETF IRA IPO ATH ATL GDP CPI PPI FED SEC FDA FTC DOJ IRS
    USA UK EU US USD EUR CAD GBP JPY CNY BTC ETH NFT OTC NYSE WSB ESG YTD YOY
    QOQ EOD EOY ITM OTM ATM DTE IV PT TA FA OP DM PM PR HR IT TV PC CEO UI UX
    RH Q1 Q2 Q3 Q4 FY TTM MOM DOD AH PDT LEAPS LEAP PLS PSA AMA ELI IIRC AFAIK
    TIL OC NSFW MOD BOT RIP GG WOW YES WHO WHY HOW WHAT WHEN ANY ALSO EVEN EVER
    """.split()
)
CASHTAG_STOPLIST = frozenset("USD EUR CAD GBP JPY AUD CNY BTC ETH".split())

CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5})(?:\.[A-Za-z])?(?![\w])")
BARE_RE = re.compile(r"(?<![\w$])([A-Z]{2,5})(?![\w])")

MEGATHREAD_RE = re.compile(
    r"daily discussion|weekend discussion|what are your moves|rate my portfolio"
    r"|moves tomorrow|daily thread|weekly .*thread|discussion thread|megathread",
    re.I,
)
FINANCE_KW_RE = re.compile(
    r"\b(revenue|margins?|valuation|earnings|10-k|10-q|guidance|moat|free cash flow"
    r"|p/e|thesis|tam|balance sheet|cash flow|gross margin|dcf|competitors?"
    r"|market share|dilution|debt)\b",
    re.I,
)
FLAIR_ALLOW = frozenset(
    {
        "dd",
        "fundamentals",
        "fundamentals/dd",
        "company discussion",
        "company analysis",
        "company news",
        "analysis",
        "discussion",
        "investing",
        "research",
        "stock analysis",
        "industry analysis",
        "industry report",
        "long thesis",
        "short thesis",
        "stock idea",
    }
)
LONGFORM_MIN_CHARS = 800
DISCUSSED_LINK_MIN_COMMENTS = 5  # link posts with at least this many comments are kept
REMOVED_MARKERS = frozenset({"[removed]", "[deleted]", "[removed by reddit]"})

POST_FIELDS = (
    "id subreddit created_utc author title selftext score upvote_ratio num_comments "
    "link_flair_text url domain permalink is_self stickied distinguished edited "
    "removed_by_category crosspost_parent retrieved_on"
).split()
COMMENT_FIELDS = (
    "id link_id parent_id created_utc author body score edited distinguished "
    "stickied retrieved_on"
).split()


# --------------------------------------------------------------------------- #
# Ticker list + matching
# --------------------------------------------------------------------------- #


def load_tickers(path: str | None, cache_dir: Path) -> set[str]:
    """One symbol per line from ``path``; else SEC's current ticker list (cached).

    The SEC list only has *current* tickers. Delisted names are still caught
    by cashtags. Swap in Massive's active+inactive list when available.
    """
    if path:
        lines = Path(path).read_text().splitlines()
        return {ln.strip().upper() for ln in lines if ln.strip() and not ln.startswith("#")}
    cache = cache_dir / "sec_company_tickers.json"
    if not cache.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(SEC_TICKERS_URL, headers={"User-Agent": SEC_USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            cache.write_bytes(r.read())
    data = json.loads(cache.read_text())
    return {str(v["ticker"]).upper().split("-")[0] for v in data.values()}


class TickerMatcher:
    def __init__(self, tickers: set[str]):
        self.tickers = frozenset(t for t in tickers if t.isalpha())

    def match(self, text: str | None) -> set[str]:
        if not text:
            return set()
        found = {
            m.upper()
            for m in CASHTAG_RE.findall(text)
            if m.upper() not in CASHTAG_STOPLIST
        }
        found.update(
            t for t in BARE_RE.findall(text) if t in self.tickers and t not in STOPLIST
        )
        return found


# --------------------------------------------------------------------------- #
# Relevance
# --------------------------------------------------------------------------- #


def _is_removed(post: dict) -> bool:
    body = (post.get("selftext") or "").strip().lower()
    return bool(post.get("removed_by_category")) or body in REMOVED_MARKERS


def is_relevant_post(
    post: dict, matcher: TickerMatcher, min_link_comments: int = DISCUSSED_LINK_MIN_COMMENTS
) -> tuple[bool, str, set[str]]:
    """Return ``(keep, reason, tickers)``. Reasons are stable strings for stats.

    Link posts (articles, letters, podcasts) often name a company without a ticker, so
    one with an active discussion is kept as ``discussed_link``. ``num_comments`` is a
    final, hindsight count; that is fine for deciding what to *store*, but must never be
    used as a signal in a backtest.
    """
    title = post.get("title") or ""
    if post.get("stickied") or MEGATHREAD_RE.search(title):
        return False, "megathread", set()

    removed = _is_removed(post)
    body = "" if removed else (post.get("selftext") or "")
    title_tickers = matcher.match(title)
    if removed and not title_tickers:
        return False, "removed", set()

    tickers = title_tickers | matcher.match(body)
    if tickers:
        return True, "ticker", tickers

    flair = (post.get("link_flair_text") or "").strip().lower()
    if flair in FLAIR_ALLOW:
        return True, "flair", tickers

    if len(body) >= LONGFORM_MIN_CHARS and len(set(FINANCE_KW_RE.findall(body.lower()))) >= 2:
        return True, "longform", tickers

    if (
        post.get("is_self") is False
        and int(post.get("num_comments") or 0) >= min_link_comments
    ):
        return True, "discussed_link", tickers

    return False, ("no_text" if not body.strip() else "no_signal"), set()


def is_empty_comment(comment: dict) -> bool:
    """Deleted/removed comments carry no information (and shouldn't be retained)."""
    body = (comment.get("body") or "").strip().lower()
    return not body or body in REMOVED_MARKERS


def slim(record: dict, fields: list[str], tickers: set[str] | None = None) -> dict:
    out = {k: record[k] for k in fields if k in record}
    if tickers is not None:
        out["matched_tickers"] = sorted(tickers)
    return out


def utc_date(created_utc) -> str:
    return datetime.fromtimestamp(int(float(created_utc)), tz=timezone.utc).strftime("%Y-%m-%d")


class MentionCounter:
    """Daily ticker mentions over *all* content, including what gets dropped.

    Needed later for crowd-saturation and mention-count baselines. Each ticker
    counts once per post/comment.
    """

    def __init__(self):
        self.counts: Counter[tuple[str, str, str, str]] = Counter()

    def add(self, created_utc, subreddit: str, tickers: set[str], source: str) -> None:
        if not tickers:
            return
        day = utc_date(created_utc)
        for t in tickers:
            self.counts[(day, subreddit, t, source)] += 1

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        with open(tmp, "w") as fh:
            fh.write("date,subreddit,ticker,source,count\n")
            for (day, sub, t, src), n in sorted(self.counts.items()):
                fh.write(f"{day},{sub},{t},{src},{n}\n")
        tmp.replace(path)
