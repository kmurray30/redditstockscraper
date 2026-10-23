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

``prompt_version`` changes when the prompt text, the Jev questions or this builder change; it
is part of the cache path and of the scorer's ledger config, so a prompt change can never reuse
old results. Prompts live in a registry (``PROMPTS``): ``thesis_v1`` (component grades and a
stance) and ``thesis_v2`` (the same plus the model's own final ``direction`` 1-5 and
``credibility`` 1-10). ``THESIS_PROMPT`` selects one for every job; the default stays v1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from entity_resolver import Mention, blind_text  # noqa: E402
from llm import Request  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "prompts"
BUILDER_VERSION = 2   # 2: all comments, final text (no 24h lock)
DEFAULT_PROMPT = "thesis_v1"   # flipped only after a newer prompt's gold-set numbers are in
LEGACY_PROMPT = "thesis_v1"    # the one prompt whose output files carry no suffix (a literal, so a
                               # later default flip never renames them)


def prompt_name(env: dict | None = None) -> str:
    """The prompt in use: ``THESIS_PROMPT`` or the default. An unknown name is refused."""
    name = (os.environ if env is None else env).get("THESIS_PROMPT") or DEFAULT_PROMPT
    if name not in PROMPTS:
        raise SystemExit(f"unknown prompt {name!r}; prompts: {', '.join(sorted(PROMPTS))}")
    return name


def spec(prompt: str | None = None) -> "PromptSpec":
    name = prompt or prompt_name()
    if name not in PROMPTS:
        raise SystemExit(f"unknown prompt {name!r}; prompts: {', '.join(sorted(PROMPTS))}")
    return PROMPTS[name]


def prompt_tag(prompt: str | None = None) -> str:
    """The suffix output files carry for a prompt other than the legacy one: "" or ".thesis_v2"."""
    name = spec(prompt).name
    return "" if name == LEGACY_PROMPT else f".{name}"


def prompt_text(prompt: str | None = None) -> str:
    return spec(prompt).file.read_text()


def prompt_version(model: str | None = None, prompt: str | None = None) -> str:
    """``<prompt>.<hash of its text>.b<builder version>``; for a Jev model also a hash of its
    questions, since Jev grades through the rubric levels rather than the JSON schema."""
    s = spec(prompt)
    tag = hashlib.sha1(s.file.read_text().encode()).hexdigest()[:6]
    v = f"{s.name}.{tag}.b{BUILDER_VERSION}"
    if model and "jev" in model.lower().split("/")[-1]:
        v += ".jev" + hashlib.sha1(json.dumps(s.questions(), sort_keys=True).encode()).hexdigest()[:6]
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


# ------------------------------------------------------------------ thesis_v2: two final judgements
#
# The second prompt (user request 2026-10-23) keeps every component grade and adds two final
# scalars the model gives itself: the thread's net DIRECTION on a 1-5 scale (the post weighed
# with the substantive comments) and its CREDIBILITY on a 1-10 scale whose ten levels are the
# user's own rating rules (the poster is biased until independent commenters test the post; a
# good write-up with no corroboration is "a 5 at best"; several credible independent voices with
# their own evidence is a 9). On Jev each is a ``score`` question over those levels; the chat
# schema takes them as integers. v1's text, schema and questions are not touched.

SCHEMA_V2 = json.loads(json.dumps(SCHEMA))
SCHEMA_V2["properties"]["direction"] = {"type": "integer", "minimum": 1, "maximum": 5}
SCHEMA_V2["properties"]["credibility"] = {"type": "integer", "minimum": 1, "maximum": 10}
SCHEMA_V2["required"] += ["direction", "credibility"]
SCHEMA_V2_WHY = json.loads(json.dumps(SCHEMA_V2))
SCHEMA_V2_WHY["properties"]["why"] = {"type": "string"}
SCHEMA_V2_WHY["required"].append("why")

JEV_DIRECTION_LEVELS = _levels(
    "Strong bear: a clear, argued case that the company is overvalued or in trouble, and the comments do not undo it.",
    "Bear: the thread leans against the company, hedged, or with real pushback in the comments.",
    "Neutral: no direction, a question, mixed views, or bull and bear sides evenly matched.",
    "Bull: the thread leans in favour of the company, hedged, or with real pushback in the comments.",
    "Strong bull: a clear, argued case for the company, and the comments do not undo it.")
# The first wording (judgement phrases, kept). A second wording that named countable features
# per level (2026-10-23) spread the answers over more levels but agreed worse with the user's
# ratings (rho 0.48 against 0.65), so it was dropped after the one round the plan allowed.
JEV_CREDIBILITY_LEVELS = _levels(
    "Not about the company as an investment, or a bare link, meme or list with nothing to judge.",
    "A question or a thin remark about the company, and no comment gives a useful answer.",
    "An opinion with nothing to check: no reasoning, no evidence, and comments that add nothing.",
    "Price-action talk or vibes with a sketch of a reason or a number here and there; no sources, and the comments do not test it.",
    "A decent, informed write-up with reasoning and some evidence but no independent corroboration in the comments: the poster's word alone.",
    "An informed post that the comments test only a little, or post and commenters tear each other apart with substance on both sides and neither is established.",
    "An informed post and informed, level comments that agree on the facts and add their own reasoning; or a thin post carried by one excellent, informative top comment.",
    "A strong write-up with evidence and a strong top comment that checks or extends it, both informative and level.",
    "Several credible, independent commenters corroborate the thread's view with their own evidence; a careful reader would act on it.",
    "Primary-source research corroborated by several independent voices with their own evidence, the strongest objections answered; nothing is left unchecked.")


def jev_questions_v2() -> dict:
    """v1's questions plus the two final judgements (ids match ``SCHEMA_V2``)."""
    q = jev_questions()
    q["direction"] = {"type": "score",
                      "instructions": "Taking the post and the substantive comments together, which way does "
                                      "the thread's case on the company named under COMPANY UNDER EVALUATION "
                                      "point?",
                      "criteria": JEV_DIRECTION_LEVELS}
    q["credibility"] = {"type": "score",
                        "instructions": "How far could a careful reader trust this thread as research on the "
                                        "company, whichever way it points? The poster came in with a goal and "
                                        "is assumed biased; independent, substantive comments are what check "
                                        "the post.",
                        "criteria": JEV_CREDIBILITY_LEVELS}
    return q


def _level(a: dict) -> int:
    """The level Jev puts the most probability on (0-based); ``round(score)`` when it gave no
    distribution (the fake backend). Probabilities are keyed by the level's index as a string."""
    probs = a.get("probabilities") or {}
    if probs:
        return int(max(probs, key=lambda k: float(probs[k])))
    return int(round(float(a["score"])))


def decode_jev_v2(answers: dict) -> dict:
    """``decode_jev`` plus the two final scalars: ``direction`` 1..5 and ``credibility`` 1..10 as
    probability-weighted floats (``1 + score``), and ``direction_5`` / ``credibility_10`` as the
    most probable level (integers, for the grid and the gold table). ``confidence`` now averages
    twelve score questions."""
    out = decode_jev(answers)
    out["direction"] = round(float(answers["direction"]["score"]) + 1, 3)
    out["credibility"] = round(float(answers["credibility"]["score"]) + 1, 3)
    out["direction_5"] = _level(answers["direction"]) + 1
    out["credibility_10"] = _level(answers["credibility"]) + 1
    return out


def derive_levels(g: dict) -> dict:
    """Fill ``direction_5`` / ``credibility_10`` from ``direction`` / ``credibility`` when a result
    (a chat model's integers, or an older cache) carries the values but not the levels. Returns
    ``g`` itself; a v1 result passes through unchanged."""
    for k, lv, hi in (("direction", "direction_5", 5), ("credibility", "credibility_10", 10)):
        if g.get(k) is not None and g.get(lv) is None:
            g[lv] = max(1, min(hi, int(round(float(g[k])))))
    return g


@dataclass(frozen=True)
class PromptSpec:
    """One prompt: its text file, the chat schema (with and without ``why``), and the Jev
    questions and decoder that restate it. ``prompt_version`` hashes the file and the questions,
    so each prompt has its own cache directory and its own ledger identity."""
    name: str
    schema: dict
    schema_why: dict
    questions: object       # () -> dict
    decode: object          # answers -> result

    @property
    def file(self) -> Path:
        return PROMPT_DIR / f"{self.name}.md"


PROMPTS: dict[str, PromptSpec] = {
    "thesis_v1": PromptSpec("thesis_v1", SCHEMA, SCHEMA_WHY, jev_questions, decode_jev),
    "thesis_v2": PromptSpec("thesis_v2", SCHEMA_V2, SCHEMA_V2_WHY, jev_questions_v2, decode_jev_v2),
}


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
    also_in: tuple = ()           # the other subreddits a merged cross-post appeared in

    @classmethod
    def from_view(cls, post: dict, comments: list[dict], name: str | None = None, *,
                  votes=None, also_in=()) -> "Thread":
        return cls(post_id=post["post_id"], cik=post["cik"], ticker=post["ticker"], name=name,
                   created_utc=int(post["created_utc"]), subreddit=post["subreddit"],
                   flair=post.get("flair"), title=post.get("title") or "",
                   body=post.get("selftext") or "", url=post.get("url"),
                   is_self=bool(post.get("is_self")), author=post.get("author"),
                   votes=post.get("votes") if votes is None else votes, comments=comments,
                   also_in=tuple(s for s in also_in if s and s != post["subreddit"]))


def merge_comments(comments: list[dict], primary: str, copies) -> list[dict]:
    """Comments of a merged cross-post read as one thread: a top-level comment under any
    copy hangs under the primary post."""
    others = {f"t3_{pid}" for pid in copies if pid != primary}
    if not others:
        return list(comments)
    return [dict(c, parent_id=f"t3_{primary}") if c.get("parent_id") in others else c for c in comments]


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
    also = f" (also posted in {', '.join('r/' + s for s in t.also_in)})" if t.also_in else ""
    return (f"AS-OF DATE: {as_of} (UTC). Use nothing from after this date.\n"
            f"COMPANY UNDER EVALUATION: {company}\n"
            f"SUBREDDIT: r/{t.subreddit}{also}{flair}{votes}{link}\n\n"
            f"TITLE: {title}\n\nBODY:\n{body or '(no text)'}\n\n{comments}\n")


def build_request(t: Thread, blinded: bool, why: bool = False, p: dict = PRUNE,
                  backend: str = "chat", prompt: str | None = None) -> Request:
    """``backend="jev"`` attaches the prompt's rubric questions and decoder (``why`` is then
    ignored: Jev gives no reasons). ``prompt`` names the prompt; default ``THESIS_PROMPT`` or v1."""
    s = spec(prompt)
    key, system, user = f"{t.post_id}_{t.cik}", prompt_text(s.name), user_message(t, blinded, p)
    if backend == "jev":
        return Request(key=key, system=system, user=user, schema=s.schema, schema_name="thesis_grade",
                       max_out=0, questions=s.questions(), decode=s.decode)
    return Request(key=key, system=system, user=user, schema=s.schema_why if why else s.schema,
                   schema_name="thesis_grade", max_out=MAX_OUT_WHY if why else MAX_OUT)


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
