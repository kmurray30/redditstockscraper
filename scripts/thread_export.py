"""Export every graded thread, with the exact text the model saw, for the thread browser.

    python scripts/thread_export.py [--variant identified] [--push]

For each row of ``data/features/thesis_threads_<variant>.parquet`` the user message is read
back from the LLM cache (``data/llm/cache/<model>/<prompt>/<variant>/<post>_<cik>.json``
keeps it under ``user``), parsed into title, body and comments with ``gold_page.parse_message``,
and written next to the grades, the thread quality ``Q`` of the default scorer, and the
company's 5-year outcome from the thread's first cutoff month when the labels hold it.
Output: ``data/ui/threads.json.zst`` (one JSON list, zstd); ``--push`` copies it to the
bucket under the same key, where ``thread_server.py`` reads it at start. The file holds
Reddit text: it stays in the bucket and on the password-gated service, nowhere public.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gold_page as gp  # noqa: E402
import llm  # noqa: E402
import thesis_scorer as ts  # noqa: E402

KEY = "data/ui/threads.json.zst"
GRADE_KEYS = ("argument_quality", "evidence_quality", "specificity", "tone_stability", "falsifiability",
              "balance", "hype_risk", "independent_support", "independent_opposition",
              "strongest_objection_severity", "n_independent_voices")
OUTCOME_KEYS = ("status", "max_mult", "excess", "perm_loss", "cagr")
GATED = ts.Params(quality="gated")


def load_outcomes(path: Path, years: int = 5) -> dict[tuple[str, str], dict]:
    """(month, cik) -> the company's outcome over ``years`` from that cutoff."""
    if not path.exists():
        return {}
    t = pq.read_table(path, columns=["month", "cik", "years", *OUTCOME_KEYS])
    out = {}
    for r in t.to_pylist():
        if int(r["years"]) == years:
            out[(r["month"], r["cik"])] = {k: r[k] for k in OUTCOME_KEYS}
    return out


def record(g: dict, user: str | None, outcome: dict | None, params: ts.Params) -> dict:
    """One browser record from a grade row and the cached message."""
    msg = gp.parse_message(user) if user else {}
    return {"key": f"{g['post_id']}_{g['cik']}", "post_id": g["post_id"], "cik": g["cik"],
            "ticker": g.get("ticker"), "name": g.get("name"), "subreddit": g.get("subreddit"),
            "created_utc": g.get("created_utc"), "first_month": g.get("first_month"),
            "votes": g.get("votes"), "n_comments": g.get("n_comments"), "n_companies": g.get("n_companies"),
            "late_shown": g.get("late_shown"), "edited_late": g.get("edited_late"),
            "is_thesis": g.get("is_thesis"), "is_thesis_p": g.get("is_thesis_p"),
            "stance": g.get("stance"), "horizon": g.get("horizon"), "confidence": g.get("confidence"),
            "q": round(ts.thread_quality(g, params), 1),
            "q_gated": round(ts.thread_quality(g, GATED), 1),
            "copies": list(g.get("copies") or [g["post_id"]]), "subreddits": list(g.get("subreddits") or [g.get("subreddit")]),
            "grades": {k: g.get(k) for k in GRADE_KEYS},
            "as_of": msg.get("as_of", ""), "flair": msg.get("flair", ""), "link": msg.get("link", ""),
            "title": msg.get("title", ""), "body": msg.get("body", ""),
            "comments": msg.get("comments", []), "comments_note": msg.get("comments_note", ""),
            "text_missing": user is None, "outcome": outcome}


def export(grades: list[dict], llm_dir: Path, variant: str, outcomes: dict, log=print) -> list[dict]:
    params = ts.Params()
    caches: dict[tuple, llm.Cache] = {}
    out, missing = [], 0
    for g in grades:
        ck = (g.get("model"), g.get("prompt_version"))
        if ck not in caches:
            caches[ck] = llm.Cache(llm.cache_dir(llm_dir, ck[0], ck[1], variant))
        path = caches[ck].path(f"{g['post_id']}_{g['cik']}")
        user = None
        if path.exists():
            try:
                user = json.loads(path.read_text()).get("user")
            except ValueError:
                user = None
        if user is None:
            missing += 1
        out.append(record(g, user, outcomes.get((g.get("first_month"), g["cik"])), params))
    log(f"[export] {len(out):,} threads, {missing:,} without a cached text, "
        f"{sum(1 for r in out if r['outcome']):,} with a 5y outcome")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--features", default="data/features")
    p.add_argument("--variant", default="identified")
    p.add_argument("--llm-dir", default="data/llm")
    p.add_argument("--labels", default="data/features/labels.parquet")
    p.add_argument("--out", default=KEY)
    p.add_argument("--push", action="store_true", help="copy the file to the bucket under the same key")
    a = p.parse_args(argv)
    grades = ts.load_grades(Path(a.features), a.variant)
    rows = export(grades, Path(a.llm_dir), a.variant, load_outcomes(Path(a.labels)))
    data = zstd.ZstdCompressor(level=9).compress(json.dumps(rows, ensure_ascii=False).encode())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"[export] wrote {out} ({len(data) / 1e6:.1f} MB compressed)", flush=True)
    if a.push:
        import bucket_sync as bs
        bs.Bucket(bs.load_creds(), workers=2).put_bytes(str(out), data)
        print(f"[export] pushed to the bucket as {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
