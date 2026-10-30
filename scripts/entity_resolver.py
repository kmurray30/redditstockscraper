#!/usr/bin/env python3
"""Entity resolver: which companies does a piece of Reddit text refer to, as of its date?

Evidence, strongest first (the method is recorded on every mention):

1. ``cashtag``       - ``$NET``. Always counts if the ticker was a listed stock that day.
2. ``bare_clean``    - an ALL-CAPS 3-5 letter token that is a listed ticker that day and not
                       an ordinary word (see ``data/ref/word_like.txt``, built by ``wordstats``).
3. ``bare_context``  - a short (2 letter) or word-like token, accepted only with finance
                       context in the same text ("shares", "calls", "earnings", ...) or the
                       company's name alongside.
4. ``name``          - the company's registered name as of that date ("Palantir Technologies";
                       a single common word like "Apple" needs capitalisation and context).
                       Posts only.

A ticker means nothing without a date: it resolves through the point-in-time security
master, so "META" before 2022 is not Facebook, "BBBY" in 2022 is the old Bed Bath & Beyond,
and "XYZ" before 2025 is not a company at all (it simply fails to resolve).

Also here: ``blind_text``, which replaces a company's ticker and names with neutral
placeholders for the ticker-blinded half of the LLM-hindsight test.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import names  # noqa: E402
from reddit_filter import (  # noqa: E402
    BARE_RE, CASHTAG_RE, CASHTAG_STOPLIST, STOPLIST, _shouting, load_stoplist,
)
from security_master import SecurityMaster  # noqa: E402

# the corpus says "BRK"; the exchange lists BRK.A / BRK.B
TICKER_ALIASES = {"BRK": ("BRK.B", "BRK.A")}
STOCK_TYPES = {"CS", "ADRC", "ETF", "ETN", "FUND", None}
NAME_TYPES = {"CS", "ADRC"}
METHOD_RANK = {"cashtag": 0, "bare_clean": 1, "bare_context": 2, "name": 3}

CONTEXT_RE = re.compile(
    r"\b(stocks?|shares?|calls?|puts?|options?|earnings|revenue|eps|guidance|buy(?:ing)?|"
    r"bought|sell(?:ing)?|sold|long|short|bullish|bearish|dividends?|ipo|valuation|"
    r"market cap|price target|position|portfolio|etf|p/e|margins?|quarter|10-?[kq]|analysts?|"
    r"upside|downside|undervalued|overvalued|ticker|invest(?:ed|ing|ment)?|moat|fcf|ebitda)\b",
    re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&'-]*")
MAX_NGRAM = 4


@dataclass
class Mention:
    cik: str
    ticker: str
    name: str | None
    sec_type: str | None
    method: str
    resolve_conf: str
    count: int = 1
    in_title: bool = False
    tickers: set = field(default_factory=set)


def load_common_words(path: Path | None) -> frozenset[str]:
    if path and Path(path).exists():
        return frozenset(ln.strip().lower() for ln in Path(path).read_text().splitlines()
                         if ln.strip() and not ln.startswith("#"))
    return frozenset()


def load_word_like(path: Path | None) -> frozenset[str]:
    if path and Path(path).exists():
        return load_stoplist(str(path))
    return frozenset()


class EntityResolver:
    def __init__(self, master: SecurityMaster, *, word_like: frozenset[str] = frozenset(),
                 extra_stoplist: frozenset[str] = frozenset(),
                 common_words: frozenset[str] = frozenset()):
        self.master = master
        self.word_like = word_like
        self.common_words = common_words
        self.stoplist = STOPLIST | extra_stoplist
        self._resolve_cache: dict = {}
        self._alias_cache: dict[str, dict] = {}

    # ------------------------------------------------------------ lookups

    def _resolve(self, ticker: str, on: str):
        key = (ticker, self.master.snapshot_index(on))
        if key not in self._resolve_cache:
            res = None
            for t in TICKER_ALIASES.get(ticker, (ticker,)):
                r = self.master.resolve(t, on)
                if r.cik is not None:
                    res = r
                    break
            self._resolve_cache[key] = res
        return self._resolve_cache[key]

    def aliases(self, on: str) -> dict[str, dict]:
        """alias -> {cik: (ticker, name, type, distinctive)} from the snapshot at or before
        ``on`` (the earliest one for older dates)."""
        i = self.master.snapshot_index(on)
        as_of = self.master.dates[max(i - 1, 0)]
        if as_of not in self._alias_cache:
            idx: dict[str, dict] = defaultdict(dict)
            for ticker, row in self.master._by_date[as_of].items():
                if row.get("type") not in NAME_TYPES or not row.get("cik"):
                    continue
                for alias, distinctive in names.alias_candidates(row.get("name")):
                    if not names.usable_alias(alias, self.common_words):
                        continue
                    idx[alias][row["cik"]] = (ticker, row.get("name"), row.get("type"), distinctive)
            self._alias_cache[as_of] = dict(idx)
        return self._alias_cache[as_of]

    # ------------------------------------------------------------ resolution

    def find(self, text: str | None, on: str | datetime, *, use_names: bool = True) -> list[Mention]:
        """Companies mentioned in ``text``, as of the date ``on`` (YYYY-MM-DD)."""
        if not text:
            return []
        on = on if isinstance(on, str) else on.strftime("%Y-%m-%d")
        ctx = bool(CONTEXT_RE.search(text))
        found: dict[str, Mention] = {}

        def add(ticker: str, res, method: str, n: int = 1):
            m = found.get(res.cik)
            if m is None:
                found[res.cik] = Mention(res.cik, ticker, res.name, res.sec_type, method,
                                         res.confidence, n, tickers={ticker})
            else:
                m.count += n
                m.tickers.add(ticker)
                if METHOD_RANK[method] < METHOD_RANK[m.method]:
                    m.method = method

        for t in CASHTAG_RE.findall(text):
            t = t.upper()
            if t in CASHTAG_STOPLIST:
                continue
            res = self._resolve(t, on)
            if res is not None and res.sec_type in STOCK_TYPES:
                add(t, res, "cashtag")

        if not _shouting(text):
            for t in BARE_RE.findall(text):
                if t in self.stoplist:
                    continue
                res = self._resolve(t, on)
                if res is None or res.sec_type not in STOCK_TYPES:
                    continue
                if res.confidence == "from_later_snapshot":
                    continue  # a bare token is too weak to trust against a later snapshot
                if len(t) >= 3 and t not in self.word_like:
                    add(t, res, "bare_clean")
                elif ctx:
                    add(t, res, "bare_context")

        if use_names:
            self._find_names(text, on, ctx, add)
        return list(found.values())

    def _find_names(self, text: str, on: str, ctx: bool, add) -> None:
        index = self.aliases(on)
        words = [(m.group(0), m.group(0).lower()) for m in _WORD.finditer(text)]
        lower = [w[1] for w in words]
        for i in range(len(lower)):
            for n in range(min(MAX_NGRAM, len(lower) - i), 0, -1):
                owners = index.get(" ".join(lower[i:i + n]))
                if not owners:
                    continue
                if len(owners) > 1:  # two companies share the alias: don't guess
                    break
                cik, (ticker, nm, typ, distinctive) = next(iter(owners.items()))
                if not distinctive and not (ctx and words[i][0][0].isupper()):
                    break
                res = self._resolve(ticker, on)
                if res is not None and res.cik == cik:
                    add(ticker, res, "name")
                break


# ---------------------------------------------------------------- blinding

def blind_text(text: str, mentions: list[Mention]) -> str:
    """Replace each resolved company's tickers, cashtags and names with COMPANY_A, COMPANY_B...
    (stable per call, in order of first appearance in ``mentions``)."""
    out = text
    for k, m in enumerate(mentions):
        label = f"COMPANY_{chr(ord('A') + k % 26)}{'' if k < 26 else k // 26}"
        pats = [rf"\$?\b{re.escape(t)}\b" for t in sorted(m.tickers, key=len, reverse=True)]
        for alias in names.blind_tokens(m.name):   # the full name, and a distinctive leading word
            spaced = re.escape(alias).replace("\\ ", r"\s+")
            pats.append(rf"\b{spaced}\b")
        out = re.sub("|".join(pats), label, out, flags=re.I) if pats else out
    return out


# ---------------------------------------------------------------- building the mentions table

MENTION_SCHEMA = pa.schema([
    ("source", pa.string()), ("source_id", pa.string()), ("post_id", pa.string()),
    ("subreddit", pa.string()), ("created_utc", pa.int64()), ("cik", pa.string()),
    ("ticker", pa.string()), ("sec_type", pa.string()), ("method", pa.string()),
    ("resolve_conf", pa.string()), ("n", pa.int32()), ("in_title", pa.bool_()),
])


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def mention_rows(er: EntityResolver, kind: str, rec: dict):
    on = _day(rec["created_utc"])
    if kind == "post":
        title = er.find(rec["title"], on, use_names=True)
        body = er.find(rec["selftext"], on, use_names=True)
        merged: dict[str, Mention] = {}
        for m in title:
            m.in_title = True
            merged[m.cik] = m
        for m in body:
            if m.cik in merged:
                merged[m.cik].count += m.count
                merged[m.cik].tickers |= m.tickers
                if METHOD_RANK[m.method] < METHOD_RANK[merged[m.cik].method]:
                    merged[m.cik].method = m.method
            else:
                merged[m.cik] = m
        ms, post_id = merged.values(), rec["id"]
    else:
        ms, post_id = er.find(rec["body"], on, use_names=False), rec["post_id"]
    for m in ms:
        yield {"source": kind, "source_id": rec["id"], "post_id": post_id,
               "subreddit": rec.get("subreddit"), "created_utc": rec["created_utc"],
               "cik": m.cik, "ticker": sorted(m.tickers)[0], "sec_type": m.sec_type,
               "method": m.method, "resolve_conf": m.resolve_conf, "n": m.count,
               "in_title": m.in_title}


def build_mentions(er: EntityResolver, parquet_root: Path, out: Path, subs=None, log=print) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    total = 0
    with pq.ParquetWriter(tmp, MENTION_SCHEMA, compression="zstd") as w:
        for kind, folder in (("post", "posts"), ("comment", "comments")):
            for f in sorted((parquet_root / folder).glob("*.parquet")):
                sub = f.stem.split("__")[0]   # "<sub>__arctic" holds the 2025+ backfill of <sub>
                if subs and sub not in subs:
                    continue
                sub_rows = 0
                for batch in pq.ParquetFile(f).iter_batches(batch_size=50_000):
                    rows = []
                    for rec in batch.to_pylist():
                        if kind == "comment":
                            rec["subreddit"] = sub
                        rows.extend(mention_rows(er, kind, rec))
                    if rows:
                        w.write_table(pa.Table.from_pylist(rows, schema=MENTION_SCHEMA))
                    sub_rows += len(rows)
                total += sub_rows
                log(f"  {f.stem} {folder}: {sub_rows:,} mentions")
    tmp.replace(out)
    return total


def build_word_like(parquet_root: Path, out: Path, *, min_uses: int = 300, max_upper: float = 0.15) -> int:
    """Tokens (2-5 letters) that are mostly written in lowercase/Title case across the whole
    corpus: ordinary words, so a bare ALL-CAPS occurrence needs context to count."""
    import duckdb

    con = duckdb.connect()
    q = f"""
    with t as (
      select unnest(regexp_extract_all(txt, '\\b[A-Za-z]{{2,5}}\\b')) tok from (
        select title || ' ' || selftext txt from read_parquet('{parquet_root}/posts/*.parquet')
        union all select body from read_parquet('{parquet_root}/comments/*.parquet'))
    )
    select upper(tok) w, count(*) n, avg((tok = upper(tok))::int) up from t group by 1
    having count(*) >= {min_uses} and avg((tok = upper(tok))::int) < {max_upper}
    order by n desc"""
    words = [r[0] for r in con.sql(q).fetchall()]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# tokens mostly written in lowercase across the corpus (from `wordstats`)\n"
                   + "\n".join(sorted(words)) + "\n")
    q2 = f"""
    with t as (
      select unnest(regexp_extract_all(txt, '\\b[A-Za-z]{{3,12}}\\b')) tok from (
        select title || ' ' || selftext txt from read_parquet('{parquet_root}/posts/*.parquet')
        union all select body from read_parquet('{parquet_root}/comments/*.parquet'))
    )
    select lower(tok) w from t group by 1
    having count(*) >= 100 and avg((tok = lower(tok))::int) >= 0.5 order by 1"""
    common = [r[0] for r in con.sql(q2).fetchall()]
    out.with_name("common_words.txt").write_text(
        "# words the corpus mostly writes in lowercase: not usable as company-name aliases\n"
        + "\n".join(common) + "\n")
    return len(words)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--massive", default="data/market/massive")
    p.add_argument("--eodhd", default="data/market/eodhd")
    p.add_argument("--word-like", default="data/ref/word_like.txt")
    p.add_argument("--common-words", default="data/ref/common_words.txt")
    p.add_argument("--stoplist", default="data/ref/stoplist.txt")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wordstats", help="find ordinary-word tokens from corpus capitalisation")
    m = sub.add_parser("mentions", help="resolve every post/comment into data/features/mentions.parquet")
    m.add_argument("--out", default="data/features/mentions.parquet")
    m.add_argument("--subs", nargs="+")
    a = p.parse_args(argv)
    if a.cmd == "wordstats":
        n = build_word_like(Path(a.parquet), Path(a.word_like))
        print(f"{n:,} word-like tokens -> {a.word_like}")
        return 0
    master = SecurityMaster.load(Path(a.massive), symbols=Path(a.eodhd) / "symbols.parquet")
    er = EntityResolver(master, word_like=load_word_like(Path(a.word_like)),
                        extra_stoplist=load_stoplist(a.stoplist),
                        common_words=load_common_words(Path(a.common_words)))
    total = build_mentions(er, Path(a.parquet), Path(a.out), a.subs)
    print(f"{total:,} mentions -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
