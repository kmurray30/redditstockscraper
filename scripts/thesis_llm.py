"""Grade the planned threads with the model, collect the grades, and audit the blinded texts.

    python scripts/thesis_llm.py run --variant identified [--years 2014 2017] [--limit N] [--dry-run]
    python scripts/thesis_llm.py run --variant blinded
    python scripts/thesis_llm.py collect --variant identified     # cache -> thesis_threads_<variant>.parquet
    python scripts/thesis_llm.py --prompt thesis_v2 run ...       # a second prompt (or THESIS_PROMPT)
    python scripts/thesis_llm.py check-blind                       # ticker / name leaks in blinded texts

``run`` rebuilds each thread's text from the point-in-time view at the cutoff where the
funnel first selected it (so the comments are exactly the ones inside the maturation
window), builds one structured request per thread, and hands them to ``llm.run``, which
caches every result and enforces the spend cap. A rerun grades only what is missing.

``collect`` joins the cached grades onto ``data/features/threads.parquet`` and writes
``data/features/thesis_threads_<variant>.parquet``, which is what the scorer reads. It
prints coverage and the lifetime spend.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from pit import PITConfig, PITCorpus  # noqa: E402
from thesis_prompt import (MATURATION_HOURS, PROMPTS, Thread, blind_leaks, build_request,  # noqa: E402
                           derive_levels, merge_comments, prompt_name, prompt_version,
                           thesis_pit_config)
from thesis_scorer import grades_path  # noqa: E402

VARIANTS = ("identified", "blinded")
GRADE_FIELDS = ("is_thesis", "stance", "horizon", "argument_quality", "evidence_quality",
                "specificity", "tone_stability", "falsifiability", "balance", "hype_risk",
                "confidence",
                # thesis_v2's two final judgements (null under thesis_v1): the weighted value and
                # the most probable level
                "direction", "credibility", "direction_5", "credibility_10")
COMMENT_FIELDS = ("independent_support", "independent_opposition", "strongest_objection_severity",
                  "n_independent_voices")

GRADED_SCHEMA = pa.schema([
    ("post_id", pa.string()), ("cik", pa.string()), ("ticker", pa.string()), ("name", pa.string()),
    ("subreddit", pa.string()), ("created_utc", pa.int64()), ("matured_utc", pa.int64()),
    ("author", pa.string()), ("votes", pa.int64()), ("n_comments", pa.int32()),
    ("n_companies", pa.int16()), ("first_month", pa.string()),
    ("decided_utc", pa.int64()), ("late_comments", pa.int32()), ("late_shown", pa.int32()),
    ("edited_late", pa.bool_()),
    ("is_thesis", pa.bool_()), ("is_thesis_p", pa.float64()), ("stance", pa.float64()), ("horizon", pa.string()),
    ("argument_quality", pa.int16()), ("evidence_quality", pa.int16()), ("specificity", pa.int16()),
    ("tone_stability", pa.int16()), ("falsifiability", pa.int16()), ("balance", pa.int16()),
    ("hype_risk", pa.int16()), ("confidence", pa.float64()),
    ("independent_support", pa.int16()), ("independent_opposition", pa.int16()),
    ("strongest_objection_severity", pa.int16()), ("n_independent_voices", pa.int32()),
    ("direction", pa.float64()), ("credibility", pa.float64()),
    ("direction_5", pa.int16()), ("credibility_10", pa.int16()),
    ("model", pa.string()), ("prompt", pa.string()), ("prompt_version", pa.string()), ("variant", pa.string()),
    ("cost", pa.float64()),
    ("copies", pa.list_(pa.string())), ("subreddits", pa.list_(pa.string())), ("n_copies", pa.int16()),
])


def load_threads(path: Path, years=None, limit: int | None = None) -> list[dict]:
    rows = pq.read_table(path).to_pylist()
    if years:
        rows = [r for r in rows if int(r["first_month"][:4]) in set(years)]
    rows.sort(key=lambda r: (r["first_month"], r["post_id"]))
    return rows[:limit] if limit else rows


def open_corpus(a, votes: str = "final") -> PITCorpus:
    return PITCorpus(Path(a.parquet), Path(a.mentions),
                     thesis_pit_config(votes, a.maturation),
                     post_features=Path(a.post_features))


def threads_from_views(corpus: PITCorpus, rows: list[dict], lookback: int, log=print):
    """Yield (row, Thread) by rebuilding each thread from the view of its first cutoff."""
    from backtest import month_utc
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r["first_month"], []).append(r)
    for m in sorted(by_month):
        view = corpus.at(month_utc(m), lookback)
        for r in by_month[m]:
            posts = view.query("select * from post_mentions where post_id = ? and cik = ? limit 1",
                               [r["post_id"], r["cik"]])
            if not posts:
                log(f"  {r['post_id']}/{r['cik']}: not in the view at {m} any more; skipped")
                continue
            copies = list(r.get("copies") or [r["post_id"]])
            comments = merge_comments(view.comments_for(copies), r["post_id"], copies)
            yield r, Thread.from_view(posts[0], comments, r.get("name"),
                                      votes=r.get("votes") if len(copies) > 1 else None,
                                      also_in=r.get("subreddits") or ())


def cmd_run(a) -> int:
    cfg = llm.Config.from_env()
    rows = load_threads(Path(a.threads), a.years, a.limit)
    corpus = open_corpus(a)
    blinded = a.variant == "blinded"
    requests, meta = [], {}
    for r, t in threads_from_views(corpus, rows, a.lookback):
        req = build_request(t, blinded=blinded, backend=cfg.backend, prompt=a.prompt)
        requests.append(req)
        meta[req.key] = r
    pv = prompt_version(cfg.model, a.prompt)
    cache = llm.Cache(llm.cache_dir(Path(a.llm_dir), cfg.model, pv, a.variant))
    spend = llm.Spend(Path(a.llm_dir) / "spend.jsonl")
    print(f"[run] {len(requests):,} threads, variant {a.variant}, prompt {pv}, "
          f"cache {cache.root}", flush=True)
    try:
        results = llm.run(requests, cfg, cache, spend, dry_run=a.dry_run)
    except llm.SpendCapError as e:
        print(f"[run] {e}")
        return 3
    print(f"[run] {len(results):,} graded of {len(requests):,}; lifetime spend ${spend.total:.3f}")
    return 0 if a.dry_run or len(results) == len(requests) else 1


def flatten(rec: dict) -> dict:
    g = derive_levels(dict(rec["result"]))
    out = {k: g.get(k) for k in GRADE_FIELDS}
    c = g.get("comments") or {}
    out.update({k: c.get(k) for k in COMMENT_FIELDS})
    out["is_thesis_p"] = (g.get("_jev") or {}).get("is_thesis_p")   # Jev only; None for chat models
    out["model"], out["cost"] = rec.get("model"), float(rec.get("cost") or 0)
    return out


def cmd_collect(a) -> int:
    model = a.model or llm.Config.from_env().model
    pv = prompt_version(model, a.prompt)
    cache = llm.cache_dir(Path(a.llm_dir), model, pv, a.variant)
    rows = load_threads(Path(a.threads), a.years)
    out, missing = [], 0
    for r in rows:
        p = cache / f"{r['post_id']}_{r['cik']}.json"
        if not p.exists():
            missing += 1
            continue
        rec = json.loads(p.read_text())
        row = {k: r.get(k) for k in ("post_id", "cik", "ticker", "name", "subreddit", "created_utc",
                                     "matured_utc", "author", "votes", "n_comments", "n_companies",
                                     "first_month", "decided_utc", "late_comments", "late_shown",
                                     "edited_late", "copies", "subreddits", "n_copies")}
        row["copies"] = list(row["copies"] or [r["post_id"]])
        row["subreddits"] = list(row["subreddits"] or [r.get("subreddit")])
        row["n_copies"] = int(row["n_copies"] or 1)
        row.update(flatten(rec))
        row["prompt"], row["prompt_version"], row["variant"] = a.prompt, pv, a.variant
        out.append(row)
    dest = grades_path(Path(a.features), a.variant, a.prompt)
    pq.write_table(pa.Table.from_pylist(out, schema=GRADED_SCHEMA), dest, compression="zstd")
    spend = llm.Spend(Path(a.llm_dir) / "spend.jsonl").total
    n_thesis = sum(1 for r in out if r["is_thesis"])
    print(f"[collect] {len(out):,} graded, {missing:,} missing of {len(rows):,} planned; "
          f"{n_thesis:,} judged theses; prompt {pv}; wrote {dest}; lifetime spend ${spend:.3f}")
    return 0 if not missing else 1


def cmd_check_blind(a) -> int:
    model = a.model or llm.Config.from_env().model
    cache = llm.cache_dir(Path(a.llm_dir), model, prompt_version(model, a.prompt), "blinded")
    rows = {(r["post_id"], r["cik"]): r for r in load_threads(Path(a.threads))}
    n = leaks = 0
    examples = []
    for p in sorted(cache.glob("*.json")):
        rec = json.loads(p.read_text())
        pid, _, cik = rec["key"].rpartition("_")
        r = rows.get((pid, cik))
        if not r:
            continue
        n += 1
        hits = blind_leaks(rec["user"], r["ticker"], r.get("name"))
        if hits:
            leaks += 1
            if len(examples) < 10:
                examples.append((r["ticker"], hits))
    print(f"[check-blind] {n:,} blinded texts, {leaks:,} with a ticker or name still visible")
    for t, h in examples:
        print(f"  {t}: {h}")
    return 0 if leaks == 0 else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--threads", default="data/features/threads.parquet")
    p.add_argument("--features", default="data/features")
    p.add_argument("--llm-dir", default="data/llm")
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--post-features", default="data/features/post_features.parquet")
    p.add_argument("--lookback", type=int, default=90)
    p.add_argument("--maturation", type=int, default=MATURATION_HOURS)
    p.add_argument("--model", help="override LLM_MODEL for collect / check-blind")
    p.add_argument("--prompt", choices=sorted(PROMPTS),
                   help="which prompt to grade with (default: THESIS_PROMPT, else thesis_v1)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--variant", choices=VARIANTS, default="identified")
    r.add_argument("--years", nargs="+", type=int)
    r.add_argument("--limit", type=int)
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("collect")
    c.add_argument("--variant", choices=VARIANTS, default="identified")
    c.add_argument("--years", nargs="+", type=int, help="only these years (a pilot); the output "
                                                          "file then holds only them")
    c.set_defaults(fn=cmd_collect)
    sub.add_parser("check-blind").set_defaults(fn=cmd_check_blind)
    a = p.parse_args(argv)
    a.prompt = a.prompt or prompt_name()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
