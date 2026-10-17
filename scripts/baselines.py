#!/usr/bin/env python3
"""Deterministic baselines the real scorer must beat (no LLM, no spend).

Reddit-based:  mention count, mention count incl. comments, lexicon sentiment, structure-only
               quality proxy (DD flair, long write-ups, links to primary sources), and a
               crowding-penalised version.
Market-based:  12-1 momentum.
Null:          seeded random.

Post-level text features (sentiment, link quality, length) depend only on a post's own text,
so they are computed once (``features``) and joined into the point-in-time view. Reddit's
final scores / comment counts are never used: the dumps hold end-of-life values.

The sentiment lexicon is a small hand-made one; it is a baseline, not a research instrument.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scorer import Cohort  # noqa: E402

BULL = frozenset("""
buy bought buying long bullish undervalued cheap upside moon rocket growth beat beats strong
outperform accumulate accumulating breakout rally squeeze calls bargain opportunity soar soaring
surge surging profitable profit profits winner winners multibagger compounder moat
""".split())
BEAR = frozenset("""
sell sold selling short shorting bearish overvalued expensive downside crash plunge decline
declining miss misses weak underperform bubble dump dumping collapse bankrupt bankruptcy fraud
scam puts loser losers lawsuit dilution overpriced worthless drop dropping tank tanking
""".split())
NEGATORS = frozenset("not no never isn't aren't won't don't doesn't wasn't without".split())
_TOKEN = re.compile(r"[a-z']+")
_URL = re.compile(r"https?://[^\s)\]>\"']+", re.I)

PRIMARY_DOMAINS = ("sec.gov", "edgar", "investor.", "ir.", "investors.")
NEWS_DOMAINS = ("wsj.com", "bloomberg.com", "reuters.com", "ft.com", "cnbc.com", "barrons.com",
                "seekingalpha.com", "nytimes.com", "economist.com", "fool.com", "marketwatch.com",
                "yahoo.com", "morningstar.com", "forbes.com", "businessinsider.com")
DD_FLAIRS = ("dd", "due diligence", "fundamental", "research", "analysis", "thesis")
LONGFORM_CHARS = 1500

FEATURE_SCHEMA = pa.schema([
    ("post_id", pa.string()), ("f_bull", pa.int32()), ("f_bear", pa.int32()),
    ("f_sent", pa.float64()), ("f_primary_links", pa.int32()), ("f_news_links", pa.int32()),
    ("f_links", pa.int32()), ("f_chars", pa.int32()), ("f_dd", pa.bool_()),
])


def sentiment_counts(text: str) -> tuple[int, int]:
    """(bullish, bearish) word counts, flipping a word within two tokens after a negator."""
    toks = _TOKEN.findall(text.lower())
    bull = bear = 0
    for i, t in enumerate(toks):
        neg = any(j >= 0 and toks[j] in NEGATORS for j in (i - 1, i - 2))
        if t in BULL:
            bull, bear = (bull, bear + 1) if neg else (bull + 1, bear)
        elif t in BEAR:
            bull, bear = (bull + 1, bear) if neg else (bull, bear + 1)
    return bull, bear


def link_counts(text: str, url: str | None = None) -> tuple[int, int, int]:
    """(primary-source links, news links, all links) in the text and the post's own url."""
    urls = _URL.findall(text or "")
    if url and url.startswith("http") and "reddit.com" not in url:
        urls.append(url)
    primary = news = 0
    for u in urls:
        try:
            host = (urlparse(u).hostname or "").lower()
        except ValueError:  # malformed urls ("http://[...") occur in the wild
            host = ""
        if any(d in host for d in PRIMARY_DOMAINS) or u.lower().endswith(".pdf"):
            primary += 1
        elif any(host.endswith(d) for d in NEWS_DOMAINS):
            news += 1
    return primary, news, len(urls)


def post_features(post: dict) -> dict:
    text = f"{post['title']}\n{post['selftext']}"
    bull, bear = sentiment_counts(text)
    primary, news, links = link_counts(text, post.get("url"))
    flair = (post.get("flair") or "").lower()
    return {"post_id": post["id"], "f_bull": bull, "f_bear": bear,
            "f_sent": (bull - bear) / (bull + bear + 3), "f_primary_links": primary,
            "f_news_links": news, "f_links": links, "f_chars": len(post["selftext"]),
            "f_dd": any(k in flair for k in DD_FLAIRS)}


def build_post_features(parquet_root: Path, out: Path, log=print) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    n = 0
    with pq.ParquetWriter(tmp, FEATURE_SCHEMA, compression="zstd") as w:
        for f in sorted((Path(parquet_root) / "posts").glob("*.parquet")):
            for batch in pq.ParquetFile(f).iter_batches(batch_size=50_000,
                                                        columns=["id", "title", "selftext", "flair", "url"]):
                rows = [post_features(r) for r in batch.to_pylist()]
                w.write_table(pa.Table.from_pylist(rows, schema=FEATURE_SCHEMA))
                n += len(rows)
            log(f"  {f.stem}: {n:,} posts so far")
    tmp.replace(out)
    return n


# ---------------------------------------------------------------- cohort features

ALL_METHODS = ("cashtag", "bare_clean", "bare_context", "name")
RELIABLE_METHODS = ("cashtag", "bare_clean", "bare_context")


MAX_COMPANIES_PER_POST = 10  # posts naming more are lists / bots, not a view on any one company


def cohort_features(cohort: Cohort, methods=RELIABLE_METHODS,
                    max_companies: int = MAX_COMPANIES_PER_POST) -> dict[str, dict]:
    """Per-company counts over the visible window, for eligible companies only. Posts that
    name more than ``max_companies`` companies (ticker lists, bot digests) are ignored."""
    marks = ",".join("?" * len(methods))
    rows = cohort.view.query(f"""
        select cik,
               count(distinct post_id) n_posts,
               count(distinct post_id) filter (in_title) n_title,
               count(distinct author) n_authors,
               avg(f_sent) sent_mean, sum(f_sent) sent_sum,
               count(distinct post_id) filter (f_dd) n_dd,
               count(distinct post_id) filter (f_chars >= {LONGFORM_CHARS}) n_long,
               sum(f_primary_links) primary_links, sum(f_news_links) news_links
        from (select distinct on (cik, post_id) * from post_mentions
              where method in ({marks}) and n_companies <= {int(max_companies)})
        group by cik""", list(methods))
    comments = {r["cik"]: r["n"] for r in cohort.view.query(f"""
        select cik, count(distinct comment_id) n from comment_mentions
        where method in ({marks}) group by cik""", list(methods))}
    thread = {r["cik"]: r["n"] for r in cohort.view.query(f"""
        select m.cik, count(distinct t.comment_id) n
        from (select distinct cik, post_id from post_mentions
              where method in ({marks}) and n_companies <= {int(max_companies)}) m
        join thread_comments t on t.post_id = m.post_id group by m.cik""", list(methods))}
    out = {}
    for r in rows:
        if r["cik"] not in cohort.universe:
            continue
        r["n_comment_mentions"] = comments.get(r["cik"], 0)
        r["n_thread_comments"] = thread.get(r["cik"], 0)
        out[r["cik"]] = r
    return out


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Cross-sectional percentile rank in [0, 1], ties share the average rank."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[items[k][0]] = ((i + j) / 2) / max(n - 1, 1)
        i = j + 1
    return out


class Baseline:
    """One configurable baseline; ``kind`` picks the score."""

    KINDS = ("mentions", "mentions_all", "sentiment", "sentiment_volume", "dd", "evidence",
             "structure", "crowd_penalized", "momentum", "random")

    def __init__(self, kind: str, *, lookback_days: int = 90, maturation_hours: int = 24,
                 min_posts: int = 1, seed: int = 0, methods=RELIABLE_METHODS,
                 max_companies: int = MAX_COMPANIES_PER_POST):
        if kind not in self.KINDS:
            raise ValueError(kind)
        self.kind, self.lookback_days, self.maturation_hours = kind, lookback_days, maturation_hours
        self.min_posts, self.seed, self.methods = min_posts, seed, tuple(methods)
        self.max_companies = max_companies
        self.name, self.version = f"baseline:{kind}", "1"

    def params(self) -> dict:
        return {"min_posts": self.min_posts, "seed": self.seed, "methods": list(self.methods),
                "max_companies": self.max_companies}

    def score(self, cohort: Cohort) -> dict[str, dict[str, float]]:
        if self.kind == "momentum":
            return {c: {"score": u["mom"]} for c, u in cohort.universe.items() if u.get("mom") is not None}
        feats = cohort_features(cohort, self.methods, self.max_companies)
        feats = {c: f for c, f in feats.items() if f["n_posts"] >= self.min_posts}
        if self.kind == "random":
            return {c: {"score": int(hashlib.sha1(f"{self.seed}|{cohort.month}|{c}".encode())
                                     .hexdigest()[:12], 16) / 16 ** 12} for c in feats}
        if self.kind == "mentions":
            raw = {c: f["n_posts"] for c, f in feats.items()}
        elif self.kind == "mentions_all":
            raw = {c: f["n_posts"] + f["n_comment_mentions"] for c, f in feats.items()}
        elif self.kind == "sentiment":
            raw = {c: f["sent_mean"] for c, f in feats.items()}
        elif self.kind == "sentiment_volume":
            raw = {c: f["sent_sum"] for c, f in feats.items()}
        elif self.kind == "dd":
            raw = {c: f["n_dd"] for c, f in feats.items()}
        elif self.kind == "evidence":
            raw = {c: 2 * f["primary_links"] + f["news_links"] for c, f in feats.items()}
        else:  # structure / crowd_penalized
            parts = [percentile_ranks({c: f[k] for c, f in feats.items()})
                     for k in ("n_dd", "n_long", "primary_links", "news_links")]
            raw = {c: sum(p[c] for p in parts) / len(parts) for c in feats}
            if self.kind == "crowd_penalized":
                crowd = percentile_ranks({c: f["n_posts"] for c, f in feats.items()})
                raw = {c: raw[c] - crowd[c] for c in raw}
        return {c: {"score": float(v), "n_posts": feats[c]["n_posts"]} for c, v in raw.items()}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--out", default="data/features/post_features.parquet")
    a = p.parse_args(argv)
    n = build_post_features(Path(a.parquet), Path(a.out))
    print(f"{n:,} posts -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
