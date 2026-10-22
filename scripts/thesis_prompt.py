"""Build the LLM request for one thread (post + pruned comment tree), identified or blinded.

The text a model sees is assembled only from the point-in-time view (``pit.py``), so the
comments are every comment the thread ever got and the text is the post's final text (see
``THESIS_PIT``); threads holding later content are flagged so they can be excluded.
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
BUILDER_VERSION = 2   # 2: all comments, final text (no 24h lock)


def prompt_text() -> str:
    return PROMPT_FILE.read_text()


def prompt_version(model: str | None = None) -> str:
    """Hash of the prompt text plus the builder version; for a Jev model also the rubric, since
    Jev grades through ``JEV_RUBRIC`` rather than the JSON schema."""
    tag = hashlib.sha1(prompt_text().encode()).hexdigest()[:6]
    v = f"{PROMPT_FILE.stem}.{tag}.b{BUILDER_VERSION}"
    if model and "jev" in model.lower().split("/")[-1]:
        v += ".jev" + hashlib.sha1(json.dumps(jev_questions(), sort_keys=True).encode()).hexdigest()[:6]
    return v


# The thesis pipeline's view (user decision 2026-10-19): no lock. A post counts from the
# first cutoff after it was written, the model sees every comment the thread ever got and the
# post's final text. Threads whose graded text holds anything written after that cutoff are
# flagged (``late_shown``, ``edited_late``); ``Params.exclude_late`` drops them in the backtest
# and the calibrator. comment_lag.py measured how much that is (88% of comments within 24h).
MATURATION_HOURS = 0
THESIS_PIT = {"edited_policy": "keep", "comments": "all"}


def thesis_pit_config(votes: str = "final", maturation_hours: int = MATURATION_HOURS):
    from pit import PITConfig
    return PITConfig(maturation_hours=maturation_hours, votes=votes, **THESIS_PIT)

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

# ------------------------------------------------------------------ the same grades as Jev questions
#
# Jev (TypeSafe AI) does not write text: each question returns a probability-weighted index over
# ordered rubric levels (``score``), a yes probability (``noul``) or one of named options
# (``choice``). The levels below restate the prompt's definitions as rubrics; the decoder maps a
# five-level score to 0-100 (x25) so downstream code sees the schema above either way.

def _levels(*texts: str) -> list[str]:
    return list(texts)


JEV_RUBRIC_VERSION = 1
JEV_SCORES: dict[str, tuple[str, list[str]]] = {
    "argument_quality": ("How good is the reasoning of the post's argument about the company?", _levels(
        "No argument: a bare claim, question, link or list with no reasoning.",
        "A reason is given but the causal mechanism is vague or circular and obvious objections are ignored.",
        "A plausible mechanism with gaps or inconsistencies; objections are only mentioned.",
        "A clear causal mechanism (X changes the economics because Y), internally consistent, addressing the main objections.",
        "A clear, consistent mechanism that anticipates and answers the strongest objections with specifics.")),
    "evidence_quality": ("How much of the argument rests on primary or quantitative evidence rather than assertion?", _levels(
        "Assertion only: no sources and no numbers.",
        "Hearsay or secondary commentary (headlines, other posts, 'I heard') without checkable detail.",
        "Some checkable numbers or secondary sources, but the key claims are still asserted.",
        "Key claims rest on primary sources (filings, transcripts, product documentation, data) or quantitative evidence.",
        "Most of the argument rests on primary or quantitative evidence cited specifically enough to verify.")),
    "specificity": ("How concrete and checkable are the claims (numbers, dates, products, customers)?", _levels(
        "Entirely vague ('great company', 'going to the moon').",
        "Mostly vague with one or two concrete references.",
        "A mix of concrete, checkable claims and vague ones.",
        "Mostly concrete, checkable claims.",
        "Precise, checkable claims throughout, with figures, dates and sources.")),
    "tone_stability": ("How measured is the tone? Low for absolutist, emotional, hype or doom language.", _levels(
        "Absolutist, emotional, hype or doom language throughout ('guaranteed', 'can't lose', all caps).",
        "Mostly emotional or promotional, with little acknowledgement of uncertainty.",
        "Mixed: some measured reasoning alongside hype, urgency or dismissiveness.",
        "Measured and specific, acknowledging some of what the author does not know.",
        "Calm and specific, with explicitly bounded claims and acknowledged uncertainty throughout.")),
    "falsifiability": ("Does the author state, or can the reader infer, what would prove the thesis wrong?", _levels(
        "Nothing could prove the thesis wrong; no conditions or timeframe.",
        "Only implicit conditions a reader could guess at.",
        "The reader can infer what would prove it wrong, but the author does not say.",
        "The author states a condition or metric that would prove the thesis wrong.",
        "The author states specific, checkable conditions, metrics or dates that would prove it wrong.")),
    "balance": ("Does the post engage seriously with valuation, competition and the strongest counterargument?", _levels(
        "Ignores valuation, competition and counterarguments entirely.",
        "Mentions one of valuation, competition or a counterargument in passing.",
        "Discusses some of valuation, competition or counterarguments, but not the strongest one.",
        "Engages seriously with valuation, competition and the strongest counterargument.",
        "Engages seriously with all of them and weighs them against the thesis explicitly.")),
    "hype_risk": ("How promotional, repetitive, urgency-driven or coordinated-sounding is the content?", _levels(
        "No promotional, urgency or coordinated-sounding content.",
        "Mild enthusiasm; no urgency and no calls to buy.",
        "Some promotional language, repetition or urgency ('get in now').",
        "Clearly promotional: repeated urgency, 'guaranteed', price targets without basis, or dismissal of doubters.",
        "Reads as a pump: coordinated-sounding, urgency-driven and promotional throughout.")),
    "independent_support": ("How strong are the comments that add independent evidence or reasoning IN FAVOUR of the thesis? Agreement without new reasoning counts for nothing.", _levels(
        "No comment adds independent evidence or reasoning in favour.",
        "One comment adds a minor independent point in favour.",
        "One or two comments add substantive independent reasoning or evidence in favour.",
        "Several commenters add substantive, distinct evidence or reasoning in favour.",
        "Multiple commenters add strong, independent, primary or quantitative evidence in favour.")),
    "independent_opposition": ("How strong are the comments that add independent evidence or reasoning AGAINST the thesis? Disagreement without new reasoning counts for nothing.", _levels(
        "No comment adds independent evidence or reasoning against.",
        "One comment adds a minor independent point against.",
        "One or two comments add substantive independent reasoning or evidence against.",
        "Several commenters add substantive, distinct evidence or reasoning against.",
        "Multiple commenters add strong, independent, primary or quantitative evidence against.")),
    "strongest_objection_severity": ("How damaging is the single best objection raised in the comments, whether or not it was answered?", _levels(
        "No objection is raised in the comments.",
        "A minor quibble that does not threaten the thesis.",
        "A real objection that weakens part of the thesis.",
        "A serious objection that undermines a central claim.",
        "An objection that, if true, destroys the thesis.")),
}
JEV_STANCE_LEVELS = _levels(
    "Clearly bearish on the company.", "Leaning bearish.",
    "Neutral, mixed, or not expressing a view.", "Leaning bullish.", "Clearly bullish on the company.")
JEV_VOICES_LEVELS = [
    "No commenter contributed anything substantive and distinct."] + [
    f"{w} commenter{'s' if i else ''} contributed something substantive and distinct."
    for i, w in enumerate(["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"], start=0)
] + ["Nine or more commenters contributed something substantive and distinct."]
JEV_HORIZON = {"days": "A trade over days.", "weeks": "A view over weeks.",
               "months": "A view over months.", "years": "A multi-year investment thesis.",
               "unclear": "No timeframe can be inferred."}


def jev_questions() -> dict:
    """The grade schema as Jev questions. Ids match the schema's field names."""
    q = {"is_thesis": {"type": "noul",
                       "instructions": "Is the post an argued view about the company named under "
                                       "COMPANY UNDER EVALUATION (a thesis), as opposed to a question, "
                                       "a news link without analysis, a meme, a portfolio request or a list?",
                       "criteria": {"true": "The post argues a view about that company.",
                                    "false": "It is a question, bare link, meme, portfolio request, list, "
                                             "or mainly about other companies."}},
         "stance": {"type": "score", "instructions": "What is the author's direction on the company?",
                    "criteria": JEV_STANCE_LEVELS},
         "horizon": {"type": "choice", "instructions": "What timeframe is the author's thesis about?",
                     "criteria": JEV_HORIZON}}
    for k, (instr, levels) in JEV_SCORES.items():
        q[k] = {"type": "score", "instructions": instr, "criteria": levels}
    q["n_independent_voices"] = {"type": "score",
                                 "instructions": "How many commenters contributed something substantive "
                                                 "and distinct (not repetition or bare agreement)?",
                                 "criteria": JEV_VOICES_LEVELS}
    return q


def decode_jev(answers: dict) -> dict:
    """Jev answers -> the ``SCHEMA`` shape. Five-level scores become 0-100; stance -1..1;
    ``confidence`` is the mean of Jev's per-question confidences. Raw answers are kept under
    ``_jev`` for later analysis (probabilities, per-question confidence)."""
    def score(k, scale=25.0):
        return int(round(max(0.0, min(100.0, float(answers[k]["score"]) * scale))))
    confs = [float(a["confidence"]) for a in answers.values()
             if a.get("type") == "score" and a.get("confidence") is not None]
    out = {"is_thesis": float(answers["is_thesis"]["noul"]) >= 0.5,
           "stance": round((float(answers["stance"]["score"]) - 2.0) / 2.0, 3),
           "horizon": answers["horizon"]["choice"],
           "confidence": round(max(0.0, min(1.0, sum(confs) / len(confs))) if confs else 0.5, 3),
           "comments": {"independent_support": score("independent_support"),
                        "independent_opposition": score("independent_opposition"),
                        "strongest_objection_severity": score("strongest_objection_severity"),
                        "n_independent_voices": int(round(float(answers["n_independent_voices"]["score"])))},
           "_jev": {"is_thesis_p": round(float(answers["is_thesis"]["noul"]), 3),
                    "confidence": {k: a.get("confidence") for k, a in answers.items() if "confidence" in a},
                    "probabilities": {k: a.get("probabilities") for k, a in answers.items() if a.get("probabilities")}}}
    for k in ("argument_quality", "evidence_quality", "specificity", "tone_stability",
              "falsifiability", "balance", "hype_risk"):
        out[k] = score(k)
    return out


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
        return "COMMENTS: none."
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


def build_request(t: Thread, blinded: bool, why: bool = False, p: dict = PRUNE,
                  backend: str = "chat") -> Request:
    """``backend="jev"`` attaches the rubric questions and decoder (``why`` is then ignored:
    Jev gives no reasons)."""
    if backend == "jev":
        return Request(key=f"{t.post_id}_{t.cik}", system=prompt_text(), user=user_message(t, blinded, p),
                       schema=SCHEMA, schema_name="thesis_grade", max_out=0,
                       questions=jev_questions(), decode=decode_jev)
    return Request(key=f"{t.post_id}_{t.cik}", system=prompt_text(), user=user_message(t, blinded, p),
                   schema=SCHEMA_WHY if why else SCHEMA, schema_name="thesis_grade",
                   max_out=MAX_OUT_WHY if why else MAX_OUT)


_CASHTAG = re.compile(r"\$[A-Za-z]{1,6}\b")


def blind_leaks(text: str, ticker: str, name: str | None) -> list[str]:
    """Ticker / cashtag / name strings still present in a blinded message's content (for the
    check-blind job). The fixed header ("AS-OF DATE", "COMPANY UNDER EVALUATION", "r/sub",
    "POST VOTES") is skipped: it is template text, and tickers such as DATE or names such as
    Under Armour and Post Holdings matched it. Empty means clean."""
    content = text.split("\n\nTITLE: ", 1)[-1]
    hits = []
    if re.search(rf"\$?\b{re.escape(ticker)}\b", content):
        hits.append(ticker)
    if name:
        from names import blind_tokens
        for alias in blind_tokens(name):
            if re.search(rf"\b{re.escape(alias).replace(chr(92) + ' ', chr(92) + 's+')}\b", content, re.I):
                hits.append(alias)
    return sorted(set(hits))
