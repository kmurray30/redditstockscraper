"""The gold set: 30 single-company threads the user rates by hand, and the model comparison.

    python scripts/goldset.py build [--n 30] [--seed 0] [--keep data/gold/ratings.csv]
    python scripts/goldset.py evaluate [--ratings data/gold/ratings.csv]

``build`` samples single-company threads from the planned set (``threads.parquet``),
spread across years, subreddits, lengths and vote levels, and writes:

    data/gold/threads.md    the threads as the model would see them (identified), to read
    data/gold/ratings.csv   one row per thread: fill ``rating`` (1-10) and ``reason``

and pushes ``data/gold/`` to the bucket when bucket credentials are present, so the user
can download both from the bucket's file browser and upload the filled CSV the same way.
``--keep RATINGS_CSV`` rebuilds the same threads (after a window or prompt change) and
carries the ratings in that file over, so nothing already rated is lost.

``evaluate`` grades the rated threads with the configured model (identified, with a one-line
``why``), prints the user's rating next to the model's quality score and reason for every
thread, and the rank correlation. It is how a model and a prompt version are chosen; it
costs cents, and it never touches backtest numbers.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gold_page  # noqa: E402
import llm  # noqa: E402
import thesis_llm as tl  # noqa: E402
from thesis_prompt import MATURATION_HOURS, build_request, prompt_version, user_message  # noqa: E402,F401
from thesis_scorer import Params, thread_quality  # noqa: E402


def strata_key(r: dict, votes_median: float) -> tuple:
    y = int(r["first_month"][:4])
    return (("early" if y <= 2014 else "mid" if y <= 2016 else "late"), r["subreddit"],
            "long" if r["chars"] >= 2000 else "short",
            "hot" if (r.get("votes") or 0) >= votes_median else "cold")


def sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """Round-robin over strata so every (era, subreddit, length, votes) cell is represented."""
    rows = [r for r in rows if (r.get("n_companies") or 1) == 1]
    titled = [r for r in rows if r.get("in_title")]       # plainly about the company
    if len(titled) >= n:
        rows = titled
    if not rows:
        return []
    rng = random.Random(seed)
    med = float(np.median([r.get("votes") or 0 for r in rows]))
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        cells[strata_key(r, med)].append(r)
    for c in cells.values():
        rng.shuffle(c)
    keys = sorted(cells)
    rng.shuffle(keys)
    out = []
    while len(out) < n and any(cells[k] for k in keys):
        for k in keys:
            if cells[k] and len(out) < n:
                out.append(cells[k].pop())
    return out


def keep_rows(rows: list[dict], ratings_path: Path, n: int, seed: int, log=print,
              min_comments: int = 0) -> tuple[list[dict], dict]:
    """The threads of an existing ratings.csv, in its order, topped up to ``n`` with fresh
    samples (with at least ``min_comments`` comments) when some are no longer in
    ``threads.parquet`` or ``n`` is larger than the set. Returns (rows, old rows by key)."""
    with Path(ratings_path).open() as f:
        old = {(r["post_id"], r["cik"]): gold_page.to_ten(r) for r in csv.DictReader(f)}
    by_key = {(r["post_id"], r["cik"]): r for r in rows}
    picked = [by_key[k] for k in old if k in by_key]
    missing = [k for k in old if k not in by_key]
    if missing:
        log(f"[gold] {len(missing)} rated threads are no longer in threads.parquet and are replaced: "
            + ", ".join(f"{old[k]['ticker']} {k[0]}" for k in missing))
    if len(picked) < n:
        rest = [r for r in rows if (r["post_id"], r["cik"]) not in old
                and (r.get("n_comments") or 0) >= min_comments]
        picked += sample(rest, n - len(picked), seed)
    return picked, old


def cmd_build(a) -> int:
    rows = tl.load_threads(Path(a.threads), a.years)
    old: dict = {}
    if a.keep:
        picked, old = keep_rows(rows, Path(a.keep), a.n, a.seed, min_comments=a.min_comments)
    else:
        picked = sample([r for r in rows if (r.get("n_comments") or 0) >= a.min_comments], a.n, a.seed)
    if not picked:
        print("no single-company threads in threads.parquet; run the funnel first")
        return 1
    corpus = tl.open_corpus(a)
    gold = Path(a.gold)
    gold.mkdir(parents=True, exist_ok=True)
    md, csv_rows = [], []
    k = 0
    for r, t in tl.threads_from_views(corpus, picked, a.lookback):
        k += 1
        md.append(f"\n\n---\n\n## {k}. [{t.ticker}] {t.title.strip()[:120]}\n\n"
                  f"post_id: `{t.post_id}`   company: {t.name or t.ticker}\n\n```text\n"
                  f"{user_message(t, blinded=False)}\n```\n")
        prev = old.get((t.post_id, t.cik), {})
        csv_rows.append({"n": k, "post_id": t.post_id, "cik": t.cik, "ticker": t.ticker,
                         "title": t.title.strip()[:80], "rating": prev.get("rating", ""),
                         "reason": prev.get("reason", ""), "ts": prev.get("ts", ""),
                         "scale": prev.get("scale", "")})
    (gold / "threads.md").write_text(
        "# Gold set\n\nRate each thread 1 (worthless) to 10 (I would act on this research) in "
        "`ratings.csv`, with a few words of reason. Judge the reasoning and sourcing, not whether "
        "you remember how the stock did.\n" + "".join(md))
    with (gold / "ratings.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
        w.writeheader()
        w.writerows(csv_rows)
    print(f"[gold] wrote {k} threads to {gold}/threads.md and {gold}/ratings.csv")
    try:
        import bucket_sync as bs
        creds = bs.load_creds()
        bs.push(Path(a.gold).parent, bs.Bucket(creds), only="gold", workers=4)
        print("[gold] pushed data/gold to the bucket: download threads.md and ratings.csv from the "
              "bucket's Files tab, fill the CSV, upload it back")
    except SystemExit as e:
        print(f"[gold] not pushed to the bucket ({e}); copy data/gold/ by hand")
    if a.dump:   # print both files through the log (see dump_file.py) for the rating page
        from dump_file import chunks
        for name in ("threads.md", "ratings.csv"):
            for line in chunks(name, (gold / name).read_bytes(), 4000):
                print(line, flush=True)
    return 0


def coverage(gold: list[dict], planned: list[dict]) -> str:
    """How the gold sample sits in the planned set: comments, votes and years."""
    def q(rows, k):
        v = np.asarray([r.get(k) or 0 for r in rows], dtype=float)
        return np.percentile(v, [25, 50, 75]) if len(v) else np.zeros(3)
    def share(rows, k, lo):
        return sum(1 for r in rows if (r.get(k) or 0) >= lo) / max(len(rows), 1)
    lines = [f"gold set {len(gold)} of {len(planned):,} planned threads"]
    for k in ("n_comments", "votes", "chars"):
        g, p = q(gold, k), q(planned, k)
        lines.append(f"  {k:10s} gold quartiles {g[0]:.0f}/{g[1]:.0f}/{g[2]:.0f}   planned {p[0]:.0f}/{p[1]:.0f}/{p[2]:.0f}")
    lines.append(f"  >= 10 comments: gold {share(gold, 'n_comments', 10):.0%}, planned {share(planned, 'n_comments', 10):.0%};"
                 f"  >= 25: gold {share(gold, 'n_comments', 25):.0%}, planned {share(planned, 'n_comments', 25):.0%}")
    yg = defaultdict(int); yp = defaultdict(int)
    for r in gold:
        yg[r["first_month"][:4]] += 1
    for r in planned:
        yp[r["first_month"][:4]] += 1
    lines.append("  years: " + ", ".join(f"{y} {yg.get(y, 0)} ({yp[y] / max(len(planned), 1):.0%} of planned)"
                                          for y in sorted(yp)))
    return "\n".join(lines)


def read_ratings(path: Path) -> dict[tuple, dict]:
    out = {}
    with Path(path).open() as f:
        for row in csv.DictReader(f):
            row = gold_page.to_ten(row)
            if (row.get("rating") or "").strip():
                out[(row["post_id"], row["cik"])] = dict(row, rating=float(row["rating"]))
    return out


def spearman(x, y) -> float:
    def ranks(v):
        v = np.asarray(v, dtype=float)
        order = v.argsort()
        r = np.empty(len(v))
        r[order] = np.arange(len(v))
        # average ties
        for val in np.unique(v):
            idx = np.nonzero(v == val)[0]
            r[idx] = r[idx].mean()
        return r
    if len(x) < 3:
        return float("nan")
    return float(np.corrcoef(ranks(x), ranks(y))[0, 1])


def cmd_evaluate(a) -> int:
    ratings = read_ratings(Path(a.ratings))
    if not ratings:
        print("no ratings filled in yet")
        return 1
    allrows = tl.load_threads(Path(a.threads))
    rows = [r for r in allrows if (r["post_id"], r["cik"]) in ratings]
    print(coverage(rows, allrows))
    if a.model:
        os.environ["LLM_MODEL"] = a.model
    cfg = llm.Config.from_env()
    corpus = tl.open_corpus(a)
    requests, meta = [], {}
    for r, t in tl.threads_from_views(corpus, rows, a.lookback):
        req = build_request(t, blinded=False, why=True, backend=cfg.backend)
        requests.append(req)
        meta[req.key] = (r, t)
    cache = llm.Cache(llm.cache_dir(Path(a.llm_dir), cfg.model, prompt_version(cfg.model), "gold-why"))
    spend = llm.Spend(Path(a.llm_dir) / "spend.jsonl")
    results = llm.run(requests, cfg, cache, spend)
    params = Params()
    table, xs, ys, feats = [], [], [], []
    for key, g in results.items():
        r, t = meta[key]
        q = thread_quality(g, params)
        user = ratings[(r["post_id"], r["cik"])]
        c = g.get("comments") or {}
        table.append((user["rating"], q, g.get("stance"), g.get("is_thesis"), t.ticker,
                      len(t.comments), (g.get("why") or "")[:90], user.get("reason", "")[:60]))
        xs.append(user["rating"])
        ys.append(q)
        feats.append({"argument": g.get("argument_quality"), "specificity": g.get("specificity"),
                      "evidence": g.get("evidence_quality"), "tone": g.get("tone_stability"),
                      "falsifiability": g.get("falsifiability"), "balance": g.get("balance"),
                      "hype_risk": g.get("hype_risk"), "confidence": g.get("confidence"),
                      "support": c.get("independent_support"), "opposition": c.get("independent_opposition"),
                      "objection": c.get("strongest_objection_severity"), "voices": c.get("n_independent_voices"),
                      "n_comments": len(t.comments), "votes": t.votes or 0, "chars": len(t.body),
                      "year": t.created_utc // 31_557_600 + 1970})
    table.sort(key=lambda row: (-row[0], -row[1]))
    print(f"model {cfg.model}, prompt {prompt_version(cfg.model)}, {len(table)} rated threads"
          + (" (Jev gives no reasons)" if cfg.backend == "jev" else ""))
    print("you  Q    stance thesis ticker  cmts  model's reason | your reason")
    for u, q, s, th, tick, nc, why, reason in table:
        print(f"{u:3.0f} {q:5.1f} {s:+6.2f} {'y' if th else 'n':6s} {tick:7s} {nc:4d}  {why} | {reason}")
    rho = spearman(xs, ys)
    pear = float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 2 else float("nan")
    print(f"\nSpearman rho {rho:.2f}, Pearson r {pear:.2f}; lifetime spend ${spend.total:.3f}")
    print("\nrank correlation of each component with your rating (n = %d):" % len(xs))
    for k in feats[0]:
        v = [float(f[k] or 0) for f in feats]
        print(f"  {k:14s} {spearman(xs, v):+.2f}   median {float(np.median(v)):g}")
    out = Path(a.gold) / f"eval_{cfg.model.replace('/', '_')}.csv"
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "you", "Q", "stance", "is_thesis", *feats[0]])
        w.writeheader()
        for i, key in enumerate(results):
            r, t = meta[key]
            w.writerow({"ticker": t.ticker, "you": xs[i], "Q": round(ys[i], 1),
                        "stance": results[key].get("stance"), "is_thesis": results[key].get("is_thesis"),
                        **feats[i]})
    print(f"wrote {out}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--threads", default="data/features/threads.parquet")
    p.add_argument("--gold", default="data/gold")
    p.add_argument("--llm-dir", default="data/llm")
    p.add_argument("--parquet", default="data/corpus/parquet")
    p.add_argument("--mentions", default="data/features/mentions.parquet")
    p.add_argument("--post-features", default="data/features/post_features.parquet")
    p.add_argument("--lookback", type=int, default=90)
    p.add_argument("--maturation", type=int, default=MATURATION_HOURS)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--n", type=int, default=30)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--years", nargs="+", type=int)
    b.add_argument("--dump", action="store_true", help="also print the files as base64 chunks")
    b.add_argument("--min-comments", type=int, default=0,
                   help="only sample threads with at least this many comments (kept rows are exempt)")
    b.add_argument("--keep", metavar="RATINGS_CSV",
                   help="rebuild the same threads as this ratings.csv (e.g. after a window change) "
                        "and carry its ratings over")
    b.set_defaults(fn=cmd_build)
    e = sub.add_parser("evaluate")
    e.add_argument("--ratings", default="data/gold/ratings.csv")
    e.add_argument("--model", help="override LLM_MODEL for this evaluation (e.g. jev-1.13.0)")
    e.set_defaults(fn=cmd_evaluate)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
