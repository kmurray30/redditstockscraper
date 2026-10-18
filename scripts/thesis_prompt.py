"""Build the LLM request for one thread (post + pruned comment tree), identified or blinded.

The text a model sees is assembled only from the point-in-time view (``pit.py``), so the
comments are the ones inside the post's maturation window and nothing is edited after it.
Vote counts come from the view's ``votes`` column when the corpus was opened with
``votes="final"``; they rank which comments are shown and are printed next to each comment.

Budget rules (all in ``PRUNE``): the body keeps its head and tail when long, the comment tree
keeps the best-voted top-level comments with their best-voted replies, two levels deep, under
a character budget. Usernames are never sent: commenters are ``u1``, ``u2`` ... per thread,
with the original poster marked ``OP``.

Blinded mode replaces the company's tickers, cashtags and name aliases with ``COMPANY_A``
(``entity_resolver.blind_text``), so the model cannot grade by remembering the company.
It still sees the date, the subreddit and everything else.

``PROMPT_VERSION`` changes when the prompt text or this builder changes; it is part of the
cache path and of the scorer's ledger config, so a prompt change can never reuse old results.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from entity_resolver import Mention, blind_text  # noqa: E402
from llm import Request  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROMPT_FILE = ROOT / "prompts" / "thesis_v1.md"
BUILDER_VERSION = 1


def prompt_text() -> str:
    return PROMPT_FILE.read_text()


def prompt_version() -> str:
    tag = hashlib.sha1(prompt_text().encode()).hexdigest()[:6]
    return f"{PROMPT_FILE.stem}.{tag}.b{BUILDER_VERSION}"


PRUNE = {
    "body_head": 6000,        # chars kept from the start of a long body
    "body_tail": 2000,        # chars kept from the end
    "comment_budget": 3600,   # total chars of comments (~900 tokens)
    "comment_chars": 600,     # per comment
    "min_comment_chars": 15,  # shorter comments are noise ("this", "lol")
    "top_level": 12,          # most top-level comments considered
    "replies": 2,             # best replies kept under each
}

SCORE = {"type": "integer", "minimum": 0, "maximum": 100}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "is_thesis": {"type": "boolean"},
        "stance": {"type": "number", "minimum": -1, "maximum": 1},
        "horizon": {"type": "string", "enum": ["days", "weeks", "months", "years", "unclear"]},
        "argument_quality": SCORE, "evidence_quality": SCORE, "specificity": SCORE,
        "tone_stability": SCORE, "falsifiability": SCORE, "balance": SCORE, "hype_risk": SCORE,
        "comments": {
            "type": "object", "additionalProperties": False,
            "properties": {"independent_support": SCORE, "independent_opposition": SCORE,
                           "strongest_objection_severity": SCORE,
                           "n_independent_voices": {"type": "integer", "minimum": 0, "maximum": 1000}},
            "required": ["independent_support", "independent_opposition",
                         "strongest_objection_severity", "n_independent_voices"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["is_thesis", "stance", "horizon", "argument_quality", "evidence_quality",
                 "specificity", "tone_stability", "falsifiability", "balance", "hype_risk",
                 "comments", "confidence"],
}
SCHEMA_WHY = json.loads(json.dumps(SCHEMA))
SCHEMA_WHY["properties"]["why"] = {"type": "string"}
SCHEMA_WHY["required"].append("why")
MAX_OUT, MAX_OUT_WHY = 300, 500


@dataclass
class Thread:
    post_id: str
    cik: str
    ticker: str
    name: str | None
    created_utc: int
    subreddit: str
    flair: str | None
    title: str
    body: str
    url: str | None
    is_self: bool
    author: str | None
    votes: int | None
    comments: list[dict]          # view rows: comment_id, parent_id, created_utc, author, body, votes?

    @classmethod
    def from_view(cls, post: dict, comments: list[dict], name: str | None = None) -> "Thread":
        return cls(post_id=post["post_id"], cik=post["cik"], ticker=post["ticker"], name=name,
                   created_utc=int(post["created_utc"]), subreddit=post["subreddit"],
                   flair=post.get("flair"), title=post.get("title") or "",
                   body=post.get("selftext") or "", url=post.get("url"),
                   is_self=bool(post.get("is_self")), author=post.get("author"),
                   votes=post.get("votes"), comments=comments)


# ------------------------------------------------------------------ text assembly

def truncate_body(body: str, head: int = PRUNE["body_head"], tail: int = PRUNE["body_tail"]) -> str:
    body = body.strip()
    if len(body) <= head + tail:
        return body
    omitted = len(body) - head - tail
    return f"{body[:head]}\n[... {omitted:,} characters omitted ...]\n{body[-tail:]}"


def _votes(c: dict) -> int:
    v = c.get("votes")
    return int(v) if v is not None else 0


def prune_comments(comments: list[dict], post_id: str, op: str | None, p: dict = PRUNE) -> list[dict]:
    """Best-voted top-level comments with their best-voted replies, two levels deep, under
    the character budget. Returns rows with ``depth`` and ``label`` (u1, u2, OP) added."""
    by_parent: dict[str, list[dict]] = {}
    for c in comments:
        body = (c.get("body") or "").strip()
        if len(body) < p["min_comment_chars"] or body in ("[deleted]", "[removed]"):
            continue
        by_parent.setdefault(c.get("parent_id") or "", []).append(c)
    rank = lambda cs: sorted(cs, key=lambda c: (-_votes(c), c["created_utc"]))  # noqa: E731
    top = rank(by_parent.get(f"t3_{post_id}", []))[:p["top_level"]]
    labels: dict[str, str] = {}

    def label(author):
        if author and op and author == op:
            return "OP"
        if author not in labels:
            labels[author] = f"u{len(labels) + 1}"
        return labels[author]

    out, used = [], 0
    for c in top:
        block = [dict(c, depth=0)]
        for r in rank(by_parent.get(f"t1_{c['comment_id']}", []))[:p["replies"]]:
            block.append(dict(r, depth=1))
            for rr in rank(by_parent.get(f"t1_{r['comment_id']}", []))[:1]:
                block.append(dict(rr, depth=2))
        size = sum(min(len(b["body"]), p["comment_chars"]) + 20 for b in block)
        if used + size > p["comment_budget"] and out:
            break
        used += size
        for b in block:
            b["label"] = label(b.get("author"))
            out.append(b)
    return out


def render_comments(pruned: list[dict], total: int, p: dict = PRUNE) -> str:
    if not pruned:
        return "COMMENTS: none within the window."
    lines = [f"COMMENTS ({len(pruned)} shown of {total}; [votes] label: text; '>' marks a reply):"]
    for c in pruned:
        body = c["body"].strip().replace("\n", " ")
        if len(body) > p["comment_chars"]:
            body = body[:p["comment_chars"]] + " [...]"
        indent = "  " * c["depth"] + (">" * c["depth"] + " " if c["depth"] else "")
        votes = f"[{_votes(c)}] " if c.get("votes") is not None else ""
        lines.append(f"{indent}{votes}{c['label']}: {body}")
    return "\n".join(lines)


def _mention(t: Thread) -> Mention:
    return Mention(cik=t.cik, ticker=t.ticker, name=t.name, sec_type=None, method="cashtag",
                   resolve_conf="stable", tickers={t.ticker})


def user_message(t: Thread, blinded: bool, p: dict = PRUNE) -> str:
    as_of = datetime.fromtimestamp(t.created_utc, timezone.utc).strftime("%Y-%m-%d")
    pruned = prune_comments(t.comments, t.post_id, t.author, p)
    title, body = t.title.strip(), truncate_body(t.body, p["body_head"], p["body_tail"])
    comments = render_comments(pruned, len(t.comments), p)
    if blinded:
        m = _mention(t)
        title, body, comments = (blind_text(x, [m]) for x in (title, body, comments))
        company = "COMPANY_A (identity hidden; grade the reasoning only)"
    else:
        company = f"{t.name} ({t.ticker})" if t.name else t.ticker
    votes = f"   POST VOTES: {t.votes}" if t.votes is not None else ""
    link = "" if t.is_self or not t.url else f"\nLINK: {t.url if not blinded else '[link hidden]'}"
    flair = f"   FLAIR: {t.flair}" if t.flair else ""
    return (f"AS-OF DATE: {as_of} (UTC). Use nothing from after this date.\n"
            f"COMPANY UNDER EVALUATION: {company}\n"
            f"SUBREDDIT: r/{t.subreddit}{flair}{votes}{link}\n\n"
            f"TITLE: {title}\n\nBODY:\n{body or '(no text)'}\n\n{comments}\n")


def build_request(t: Thread, blinded: bool, why: bool = False, p: dict = PRUNE) -> Request:
    return Request(key=f"{t.post_id}_{t.cik}", system=prompt_text(), user=user_message(t, blinded, p),
                   schema=SCHEMA_WHY if why else SCHEMA, schema_name="thesis_grade",
                   max_out=MAX_OUT_WHY if why else MAX_OUT)


_CASHTAG = re.compile(r"\$[A-Za-z]{1,6}\b")


def blind_leaks(text: str, ticker: str, name: str | None) -> list[str]:
    """Ticker / cashtag / name-alias strings still present in a blinded message (for the
    check-blind job). Empty means clean."""
    hits = []
    if re.search(rf"\$?\b{re.escape(ticker)}\b", text):
        hits.append(ticker)
    if name:
        from names import alias_candidates, core_tokens
        for alias, _ in alias_candidates(name):
            if re.search(rf"\b{re.escape(alias)}\b", text, re.I):
                hits.append(alias)
        core = core_tokens(name)
        if core and re.search(rf"\b{re.escape(core[0])}\b", text, re.I):
            hits.append(core[0])
    return sorted(set(hits))
