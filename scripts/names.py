"""Company-name normalisation shared by the entity resolver and the security master."""

from __future__ import annotations

import re

# words that carry no identity in a registered name
SUFFIXES = frozenset("""
inc incorporated corp corporation cp cos co company companies ltd limited plc llc lp l p nv sa ag se
holdings holding hldgs hldg group grp the de del new class cl a b c ordinary shares share common
stock com cap adr ads american depositary depository receipts reit trust tr units unit
""".split())

# first words too generic to stand for a company on their own
GENERIC_FIRST = frozenset("""
united american first general national global international western northern southern eastern
pacific atlantic central digital new great north south west east royal premier standard
universal advanced applied allied associated consolidated continental first bank energy capital
financial healthcare health pharma pharmaceuticals systems resources industries technologies
technology solutions services partners networks media entertainment brands foods
""".split())

_TOKEN = re.compile(r"[a-z0-9&]+")
_NOISE = re.compile(r"\b(capital stock|common stock|ordinary shares?|class [a-d]|cl [a-d])\b")


_QUOTES = str.maketrans("", "", "'\u2019\u2018`")


def tokens(name: str | None) -> list[str]:
    return _TOKEN.findall(_NOISE.sub(" ", (name or "").lower().translate(_QUOTES)))


def core_tokens(name: str | None) -> list[str]:
    """Identity-bearing tokens: suffixes and share-class markers removed."""
    out = [t for t in tokens(name) if t not in SUFFIXES]
    return out or tokens(name)


def normalize_name(name: str | None) -> str:
    return " ".join(core_tokens(name))


def alias_candidates(name: str | None) -> list[tuple[str, bool]]:
    """(alias, distinctive) pairs for a registered name. Only the full normalised name is used:
    a leading word on its own ("Azure" for Azure Power Global) is too often something else.
    ``distinctive`` aliases (two or more words) are safe alone; a one-word name needs
    capitalisation and finance context."""
    core = core_tokens(name)
    if not core:
        return []
    # a single word ("apple", "target", "square") is never safe alone: it is also English
    out = [(" ".join(core), len(core) >= 2)]
    return out


# everyday words that lead some registered names ("Under Armour", "Post Holdings", "Air
# Products"): blinding them on their own would rewrite ordinary prose
COMMON_LEAD = frozenset("""
under post air date one two three big best true main home world life care star sun gold blue
green red black white open free fast next live play real super prime core key net web total
pure smart simple just good top high low long short up on in at go the a and or for of to by
""".split())


def blind_tokens(name: str | None) -> list[str]:
    """What to hide for a company name besides its tickers: the full normalised name, and its
    leading word alone when that word is identity-bearing (four letters or more, not a generic
    or everyday word). ``blind_text`` and the blinded-text leak check share this."""
    core = core_tokens(name)
    if not core:
        return []
    out = [" ".join(core)]
    lead = core[0]
    if len(core) > 1 and len(lead) >= 4 and lead not in GENERIC_FIRST and lead not in COMMON_LEAD:
        out.append(lead)
    return out


def compact(name: str | None) -> str:
    return "".join(core_tokens(name))


def name_similarity(a: str | None, b: str | None) -> float:
    """Share of identity tokens in common (0..1), or 1.0 when one compacted name contains the
    other ("Overstock.com" vs "Overstockcom Inc")."""
    ta, tb = set(core_tokens(a)), set(core_tokens(b))
    if not ta or not tb:
        return 0.0
    ca, cb = compact(a), compact(b)
    if min(len(ca), len(cb)) >= 4 and (ca.startswith(cb) or cb.startswith(ca)):
        return 1.0
    return len(ta & tb) / min(len(ta), len(tb))


# proper nouns that appear capitalised but are not the company a registered name suggests
NOT_COMPANY = frozenset("""
monday tuesday wednesday thursday friday saturday sunday january february march april may june
july august september october november december america american china chinese canada canadian
europe european asia asian japan japanese india indian russia russian germany german france
french mexico brazil australia korea korean israel ukraine taiwan england britain british
london texas california florida york new alaska hawaii nevada ohio utah georgia virginia
carolina washington nasdaq nyse dow fed federal reserve treasury congress senate bitcoin
ethereum crypto reddit internet wall street market markets bank banks bond bonds oil gold
silver energy
""".split())


def usable_alias(alias: str, common: frozenset[str]) -> bool:
    """Is this name alias specific enough to search for in free text? ``common`` holds the
    words the corpus mostly writes in lowercase ("now", "value", "news", "target")."""
    toks = alias.split()
    if not toks or alias in NOT_COMPANY:
        return False
    if len(toks) == 1:
        return len(toks[0]) >= 5 and toks[0] not in common and toks[0] not in NOT_COMPANY
    if len(toks) == 2 and all(t in common or t in NOT_COMPANY for t in toks):
        return False  # "first capital", "global energy"
    return True
