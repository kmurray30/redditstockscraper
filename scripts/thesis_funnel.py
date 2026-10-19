"""Which threads reach the LLM, and the census that prices a run before any call is made.

Most kept posts are not theses: questions, news links, memes, lists. Scoring them would
spend the budget on noise, so the funnel is strict and point-in-time:

1. A thread candidate is one (post, company) row of the view where the company was resolved
   by a reliable method (cashtag, clean or context bare ticker) and is a subject of the post,
   not a passing mention: a cashtag, or the ticker in the title, or at least two mentions
   (a volatility post that says "ATR" once is not about AptarGroup); the post names at most
   ``MAX_COMPANIES`` companies, or up to ``MAX_FOCUS_COMPANIES`` when this company is the
   focus (in the title, or the most-mentioned company of the post, at least twice: a long
   DD naturally names competitors, a portfolio list has no focus); the author is not deleted
   or a bot; and the post has substance: a self post with >= ``MIN_SELF_CHARS`` of body, a
   link post with some written text or a primary-source link, or a thin post that drew a
   real discussion (>= ``MIN_DISCUSSION_COMMENTS`` comments inside the maturation window and
   >= ``MIN_DISCUSSION_VOTES`` votes; the analysis is then in the comments). The company must
   be in the eligible universe that month (same rule as the baselines).
2. At every monthly cutoff, for every company, the top ``TOP_K`` candidates in the view
   (``LOOKBACK`` days) by a cheap pre-score are selected. The pre-score is the mean
   percentile rank of body length, links to primary and news sources, DD flair, and
   log(1 + votes). Selection uses only what the view shows at that cutoff.
3. The scored set is the union of selections over all cutoffs. A thread visible at three
   cutoffs is scored once (the LLM cache is per thread).

``plan`` writes ``data/features/threads.parquet`` (one row per selected thread, with the
company's registered name for the prompt) and prints the census: counts per stage and year,
reject reasons, an input-token estimate and the cost at the configured model's price for
both the identified and the blinded run, plus random survivors and rejects to eyeball.
Run it before setting any API key.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

from baselines import percentile_ranks  # noqa: E402
from pit import PITConfig, PITCorpus  # noqa: E402
from thesis_prompt import MATURATION_HOURS, PRUNE, prune_comments, thesis_pit_config  # noqa: E402

CANDIDATE_METHODS = ("cashtag", "bare_clean", "bare_context")
METHOD_RANK = {m: i for i, m in enumerate(CANDIDATE_METHODS + ("name",))}
MAX_COMPANIES = 2
MAX_FOCUS_COMPANIES = 6
MIN_SELF_CHARS = 400
MIN_LINK_CHARS = 200
MIN_DISCUSSION_COMMENTS = 15
MIN_DISCUSSION_VOTES = 10
TOP_K = 3
LOOKBACK = 90
BAD_AUTHORS = frozenset({"[deleted]", "AutoModerator", "None", ""})
SYSTEM_CHARS = 2600   # the system prompt plus the message header, per request
OUT_TOKENS = 180      # typical structured output

THREAD_SCHEMA = pa.schema([
    ("post_id", pa.string()), ("cik", pa.string()), ("ticker", pa.string()), ("name", pa.string()),
    ("subreddit", pa.string()), ("created_utc", pa.int64()), ("matured_utc", pa.int64()),
    ("author", pa.string()), ("flair", pa.string()), ("is_self", pa.bool_()),
    ("n_companies", pa.int16()), ("in_title", pa.bool_()), ("method", pa.string()), ("n", pa.int32()),
    ("chars", pa.int32()), ("votes", pa.int64()), ("n_comments", pa.int32()),
    ("pre_score", pa.float64()), ("first_month", pa.string()), ("n_months", pa.int16()),
    ("est_tokens", pa.int32()), ("decided_utc", pa.int64()), ("late_comments", pa.int32()),
    ("late_shown", pa.int32()), ("edited_late", pa.bool_()),
])


def reject_reason(row: dict, max_companies: int = MAX_COMPANIES, *, post_max_n: int | None = None,
                  n_comments: int = 0) -> str | None:
    """None if the row is a candidate, else why not. ``post_max_n`` is the largest mention
    count any company has in this post (for the focus rule); ``n_comments`` the comments
    inside the maturation window (for the discussion rule)."""
    if row["method"] not in CANDIDATE_METHODS:
        return "unreliable_mention"
    if not (row["method"] == "cashtag" or row.get("in_title") or (row.get("n") or 0) >= 2):
        return "passing_mention"
    nc = row.get("n_companies") or 1
    if nc > max_companies:
        n = row.get("n") or 0
        focus = bool(row.get("in_title")) or (post_max_n is not None and n >= post_max_n and n >= 2)
        if nc > MAX_FOCUS_COMPANIES or not focus:
            return "many_companies"
    if (row.get("author") or "") in BAD_AUTHORS:
        return "deleted_author"
    chars = row["f_chars"] if row.get("f_chars") is not None else len(row.get("selftext") or "")
    substantive = (chars >= MIN_SELF_CHARS if row.get("is_self")
                   else chars >= MIN_LINK_CHARS or bool(row.get("f_primary_links") or 0))
    if substantive:
        return None
    if n_comments >= MIN_DISCUSSION_COMMENTS and (row.get("votes") or 0) >= MIN_DISCUSSION_VOTES:
        return None   # a thin post whose discussion is the content
    return "thin_self_post" if row.get("is_self") else "bare_link"


def post_context(view, rows: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """Per post: the largest mention count of any company, and the comment count in the
    view (comments inside the maturation window)."""
    max_n: dict[str, int] = {}
    for r in rows:
        max_n[r["post_id"]] = max(max_n.get(r["post_id"], 0), int(r.get("n") or 0))
    n_comments = {r["post_id"]: int(r["n"]) for r in view.query(
        "select post_id, count(*) n from thread_comments group by post_id")}
    return max_n, n_comments


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per (post, company): the most reliable mention method wins."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r["post_id"], r["cik"])
        if k not in best or METHOD_RANK.get(r["method"], 9) < METHOD_RANK.get(best[k]["method"], 9):
            best[k] = r
    return list(best.values())


def pre_scores(cands: list[dict]) -> dict[tuple, float]:
    keys = [(r["post_id"], r["cik"]) for r in cands]
    parts = []
    for fn in (lambda r: r.get("f_chars") or 0,
               lambda r: 2 * (r.get("f_primary_links") or 0) + (r.get("f_news_links") or 0),
               lambda r: 1 if r.get("f_dd") else 0,
               lambda r: math.log1p(max(r.get("votes") or 0, 0))):
        parts.append(percentile_ranks({k: fn(r) for k, r in zip(keys, cands)}))
    return {k: sum(p[k] for p in parts) / len(parts) for k in keys}


def select(view, universe: dict | None, top_k: int = TOP_K,
           max_companies: int = MAX_COMPANIES) -> tuple[list[dict], Counter]:
    """Selected rows (with ``pre_score``) at this cutoff, and reject counts by reason."""
    rows = dedupe(view.posts())
    max_n, n_comments = post_context(view, rows)
    rejects: Counter = Counter()
    cands = []
    for r in rows:
        if universe is not None and r["cik"] not in universe:
            rejects["not_in_universe"] += 1
            continue
        why = reject_reason(r, max_companies, post_max_n=max_n.get(r["post_id"]),
                            n_comments=n_comments.get(r["post_id"], 0))
        if why:
            rejects[why] += 1
        else:
            cands.append(r)
    scores = pre_scores(cands) if cands else {}
    by_cik: dict[str, list[dict]] = defaultdict(list)
    for r in cands:
        r = dict(r, pre_score=scores[(r["post_id"], r["cik"])])
        by_cik[r["cik"]].append(r)
    out = []
    for cik, rs in by_cik.items():
        rs.sort(key=lambda r: (-r["pre_score"], -(r.get("votes") or 0), r["created_utc"]))
        out.extend(rs[:top_k])
        rejects["over_cap"] += max(0, len(rs) - top_k)
    return out, rejects


def est_tokens(title: str, chars: int, n_comments: int) -> int:
    body = min(chars, PRUNE["body_head"] + PRUNE["body_tail"])
    comments = min(n_comments * 150, PRUNE["comment_budget"])
    return (SYSTEM_CHARS + len(title or "") + body + comments) // 4


def first_cutoff_after(ts: int) -> int:
    """The first monthly cutoff (00:00 UTC on the 1st) at or after ``ts``: when a thread can
    first enter a decision."""
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(int(ts), timezone.utc)
    if (d.day, d.hour, d.minute, d.second) == (1, 0, 0, 0):
        return int(ts)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())


def late_content(post: dict, comments: list[dict]) -> dict:
    """What a reader at the thread's first decision cutoff could not have seen: comments
    written after it (all, and those the prompt shows), and an edit after it (or at an
    unknown time, ``edited_utc = -1``)."""
    decided = first_cutoff_after(post["created_utc"])
    shown = prune_comments(comments, post["post_id"], post.get("author"))
    ed = int(post.get("edited_utc") or 0)
    return {"decided_utc": decided,
            "late_comments": sum(1 for c in comments if int(c["created_utc"]) > decided),
            "late_shown": sum(1 for c in shown if int(c["created_utc"]) > decided),
            "edited_late": ed == -1 or ed > decided}


def plan(corpus: PITCorpus, months: list[str], universe_by_month: dict | None, *,
         lookback: int = LOOKBACK, top_k: int = TOP_K, max_companies: int = MAX_COMPANIES,
         names: dict[str, str] | None = None, log=print) -> tuple[list[dict], dict]:
    """Iterate the cutoffs like the backtest does. Returns the selected threads and the census."""
    from backtest import month_utc
    threads: dict[tuple, dict] = {}
    rejects_total: Counter = Counter()
    stage = defaultdict(Counter)   # year -> counts
    for m in months:
        view = corpus.at(month_utc(m), lookback)
        uni = universe_by_month.get(m) if universe_by_month is not None else None
        sel, rej = select(view, uni, top_k, max_companies)
        rejects_total.update(rej)
        post_ids = [r["post_id"] for r in sel if (r["post_id"], r["cik"]) not in threads]
        by_post: dict[str, list[dict]] = defaultdict(list)
        for c in view.comments_for(post_ids):
            by_post[c["post_id"]].append(c)
        n_comments = {pid: len(cs) for pid, cs in by_post.items()}
        for r in sel:
            k = (r["post_id"], r["cik"])
            if k in threads:
                threads[k]["n_months"] += 1
                continue
            chars = r["f_chars"] if r.get("f_chars") is not None else len(r.get("selftext") or "")
            threads[k] = {
                "post_id": r["post_id"], "cik": r["cik"], "ticker": r["ticker"],
                "name": (names or {}).get(r["cik"]), "subreddit": r["subreddit"],
                "created_utc": int(r["created_utc"]),
                "matured_utc": int(r["created_utc"]) + corpus.config.maturation_hours * 3600,
                "author": r.get("author"), "flair": r.get("flair"), "is_self": bool(r.get("is_self")),
                "n_companies": int(r.get("n_companies") or 1), "in_title": bool(r.get("in_title")),
                "method": r.get("method"), "n": int(r.get("n") or 0), "chars": int(chars), "votes": r.get("votes"), "n_comments": int(n_comments.get(r["post_id"], 0)),
                "pre_score": float(r["pre_score"]), "first_month": m, "n_months": 1,
                "est_tokens": est_tokens(r.get("title") or "", int(chars), int(n_comments.get(r["post_id"], 0))),
                **late_content(r, by_post.get(r["post_id"], [])),
            }
        yr = m[:4]
        stage[yr]["cohorts"] += 1
        stage[yr]["rows_seen"] += sum(rej.values()) + len(sel)   # rejects include over_cap
        stage[yr]["selected"] += len(sel)
        log(f"  {m}: {len(sel):,} selected, {sum(rej.values()):,} rejected")
    for t in threads.values():
        stage[t["first_month"][:4]]["unique_threads"] += 1
        stage[t["first_month"][:4]]["tokens"] += t["est_tokens"]
    census = {"stage": {y: dict(c) for y, c in sorted(stage.items())},
              "rejects": dict(rejects_total.most_common()), "n_threads": len(threads),
              "tokens": sum(t["est_tokens"] for t in threads.values())}
    return list(threads.values()), census


def cost(census: dict, price_in: float, price_out: float) -> dict:
    one = (census["tokens"] * price_in + census["n_threads"] * OUT_TOKENS * price_out) / 1e6
    return {"identified": one, "blinded": one, "both": 2 * one}


def format_census(threads: list[dict], census: dict, price_in: float, price_out: float,
                  model: str, seed: int = 0, rejected_samples: list[dict] | None = None) -> str:
    lines = ["year   cohorts  rows seen  selected  unique threads  est tokens"]
    for y, c in census["stage"].items():
        lines.append(f"{y}   {c.get('cohorts', 0):7,}  {c.get('rows_seen', 0):9,}  {c.get('selected', 0):8,}"
                     f"  {c.get('unique_threads', 0):14,}  {c.get('tokens', 0):10,}")
    lines.append(f"TOTAL unique threads {census['n_threads']:,}, est input tokens {census['tokens']:,}")
    lines.append("rejects (per cohort row, so a post counts once per cutoff it is visible at):")
    for k, v in census["rejects"].items():
        lines.append(f"  {k:20s} {v:,}")
    c = cost(census, price_in, price_out)
    lines.append(f"cost at {model} (${price_in}/M in, ${price_out}/M out, ~{OUT_TOKENS} out tokens each): "
                 f"identified ${c['identified']:.2f}, blinded ${c['blinded']:.2f}, both ${c['both']:.2f}")
    if threads and "late_shown" in threads[0]:
        n = len(threads)
        shown = sum(1 for t in threads if t.get("late_shown"))
        anyl = sum(1 for t in threads if t.get("late_comments"))
        ed = sum(1 for t in threads if t.get("edited_late"))
        either = sum(1 for t in threads if t.get("late_shown") or t.get("edited_late"))
        lc = sum(t.get("late_comments") or 0 for t in threads)
        nc = sum(t.get("n_comments") or 0 for t in threads)
        lines.append(f"late content (written after the thread's first cutoff): {anyl:,} threads "
                     f"({anyl / n:.0%}) have late comments, {lc:,} of {nc:,} comments ({lc / max(nc, 1):.0%}); "
                     f"{shown:,} ({shown / n:.0%}) show one in the prompt; {ed:,} ({ed / n:.0%}) edited late; "
                     f"exclude_late would drop {either:,} ({either / n:.0%})")
    rng = random.Random(seed)
    lines.append("\n10 random selected threads:")
    for t in rng.sample(threads, min(10, len(threads))):
        lines.append(f"  [{t['ticker']}] r/{t['subreddit']} {t['first_month'][:7]} votes={t['votes']} "
                     f"chars={t['chars']:,} comments={t['n_comments']} pre={t['pre_score']:.2f}")
    if rejected_samples:
        lines.append("\n10 random rejected rows:")
        for r in rng.sample(rejected_samples, min(10, len(rejected_samples))):
            lines.append(f"  [{r['ticker']}] r/{r['subreddit']} {r['reason']:18s} votes={r.get('votes')} "
                         f"chars={r.get('f_chars')} title={str(r.get('title'))[:70]!r}")
    return "\n".join(lines)


def rejected_sample(corpus, months, universe_by_month, lookback, max_companies, n=200, seed=0):
    """A sample of rejected rows with their reason, for the census print-out."""
    from backtest import month_utc
    rng = random.Random(seed)
    out = []
    for m in rng.sample(months, min(6, len(months))):
        view = corpus.at(month_utc(m), lookback)
        uni = universe_by_month.get(m) if universe_by_month is not None else None
        rows = dedupe(view.posts())
        max_n, n_comments = post_context(view, rows)
        for r in rows:
            why = ("not_in_universe" if uni is not None and r["cik"] not in uni
                   else reject_reason(r, max_companies, post_max_n=max_n.get(r["post_id"]),
                                      n_comments=n_comments.get(r["post_id"], 0)))
            if why:
                out.append(dict(r, reason=why))
    return rng.sample(out, min(n, len(out)))


def load_names(massive: Path, eodhd: Path) -> dict[str, str]:
    from security_master import SecurityMaster
    master = SecurityMaster.load(Path(massive), None, symbols=Path(eodhd) / "symbols.parquet")
    return {cik: master.name_of(cik) for cik in master._cik_names}


def main(argv=None) -> int:
    import duckdb
    from backtest import universe_by_month
    import ledger as lg
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--post-features", default="data/features/post_features.parquet")
    p.add_argument("--universe", default="data/features/universe.parquet")
    p.add_argument("--massive", default="data/market/massive")
    p.add_argument("--eodhd", default="data/market/eodhd")
    p.add_argument("--out", default="data/features/threads.parquet")
    p.add_argument("--splits", nargs="+", default=["dev"], choices=list(lg.SPLITS))
    p.add_argument("--years", nargs="+", type=int, help="limit to these cohort years")
    p.add_argument("--lookback", type=int, default=LOOKBACK)
    p.add_argument("--maturation", type=int, default=MATURATION_HOURS)
    p.add_argument("--top-k", type=int, default=TOP_K)
    p.add_argument("--max-companies", type=int, default=MAX_COMPANIES)
    p.add_argument("--model", default=None, help="for the cost line (default LLM_MODEL or gpt-5-nano)")
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    import os
    from llm import price_for
    model = a.model or os.environ.get("LLM_MODEL") or "gpt-5-nano"
    price_in, price_out = price_for(model, dict(os.environ))

    corpus = PITCorpus(Path(a.parquet), Path(a.mentions),
                       thesis_pit_config("final", a.maturation),
                       post_features=Path(a.post_features))
    con = duckdb.connect()
    con.execute(f"create view universe as select * from read_parquet('{a.universe}')")
    all_months = [r[0] for r in con.execute("select distinct month from universe order by 1").fetchall()]
    months = [m for s in a.splits for m in lg.split_months(s, all_months)]
    if a.years:
        months = [m for m in months if int(m[:4]) in a.years]
    uni = universe_by_month(con, months)
    print(f"planning {len(months)} cohorts ({months[0]} .. {months[-1]}), top {a.top_k} per company, "
          f"lookback {a.lookback}d", flush=True)
    names = load_names(Path(a.massive), Path(a.eodhd))
    threads, census = plan(corpus, months, uni, lookback=a.lookback, top_k=a.top_k,
                           max_companies=a.max_companies, names=names)
    rej = rejected_sample(corpus, months, uni, a.lookback, a.max_companies, seed=a.seed)
    print(format_census(threads, census, price_in, price_out, model, a.seed, rej))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(threads, schema=THREAD_SCHEMA), out, compression="zstd")
    print(f"wrote {len(threads):,} threads to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
