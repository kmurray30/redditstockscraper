import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import entity_resolver as er_mod  # noqa: E402
import security_master as sm  # noqa: E402


def row(as_of, ticker, cik, name, typ="CS"):
    return {"as_of": as_of, "ticker": ticker, "cik": cik, "name": name, "type": typ, "active": True}


D1, D2 = "2020-01-04", "2022-01-04"
SNAPS = []
for d in (D1, D2):
    SNAPS += [row(d, "PLTR", "2", "Palantir Technologies Inc. Class A")] if d == D2 else []
    SNAPS += [
        row(d, "AAPL", "1", "Apple Inc."),
        row(d, "NET", "3", "Cloudflare, Inc."), row(d, "GE", "4", "General Electric Company"),
        row(d, "BRK.B", "5", "Berkshire Hathaway Inc. Class B"),
        row(d, "SPY", "6", "SPDR S&P 500 ETF Trust", "ETF"),
        row(d, "WARR", "7", "Some Warrants", "WARRANT"),
    ]
SNAPS.append(row(D2, "META", "8", "Meta Platforms, Inc. Class A"))
SNAPS.append(row(D1, "FB", "8", "Facebook, Inc. Class A"))
SNAPS.append(row(D2, "BBBY", "9", "Overstock successor"))
SNAPS.append(row(D1, "BBBY", "10", "Bed Bath & Beyond Inc"))


@pytest.fixture
def er():
    master = sm.SecurityMaster(SNAPS)
    return er_mod.EntityResolver(master, word_like=frozenset({"NET", "GE"}))


def ciks(ms):
    return {m.cik: m.method for m in ms}


def test_cashtag_beats_everything(er):
    assert ciks(er.find("loading up on $NET", "2020-06-01")) == {"3": "cashtag"}


def test_bare_clean_ticker(er):
    assert ciks(er.find("AAPL earnings were strong", "2020-06-01")) == {"1": "bare_clean"}


def test_word_like_bare_ticker_needs_context(er):
    assert ciks(er.find("what is the NET effect of this", "2020-06-01")) == {}
    assert ciks(er.find("I bought NET shares last week", "2020-06-01")) == {"3": "bare_context"}


def test_ticker_that_did_not_exist_yet_is_not_a_company(er):
    assert er.find("PLTR to the moon", "2020-06-01") == []          # snapshot 2020 has no PLTR
    # as a cashtag it is accepted (an IPO between snapshots), but flagged as such
    m = er.find("$PLTR to the moon", "2020-06-01")
    assert ciks(m) == {"2": "cashtag"} and m[0].resolve_conf == "from_later_snapshot"
    assert ciks(er.find("PLTR to the moon", "2022-06-01")) == {"2": "bare_clean"}


def test_ticker_reuse_resolves_by_date(er):
    assert ciks(er.find("$BBBY squeeze", "2020-06-01")) == {"10": "cashtag"}
    assert ciks(er.find("$BBBY squeeze", "2022-06-01")) == {"9": "cashtag"}


def test_meta_is_facebook_only_after_the_rename(er):
    assert er.find("META is cheap, stock looks good", "2020-06-01") == []
    assert er.find("$META is cheap", "2020-06-01") == []  # FB existed then: a later rename
    assert ciks(er.find("$META is cheap", "2022-06-01")) == {"8": "cashtag"}
    assert ciks(er.find("$FB is cheap", "2020-06-01")) == {"8": "cashtag"}


def test_brk_maps_to_berkshire(er):
    assert ciks(er.find("BRK is my biggest position", "2020-06-01")) == {"5": "bare_clean"}


def test_warrants_and_non_stocks_are_ignored(er):
    assert er.find("$WARR", "2020-06-01") == []


def test_etfs_are_found_but_labelled(er):
    m = er.find("buying SPY puts", "2020-06-01")
    assert [(x.cik, x.sec_type) for x in m] == [("6", "ETF")]


def test_stoplisted_word_is_never_bare(er):
    assert er.find("THE USA IS BIG", "2020-06-01") == []


def test_shouting_text_gets_no_bare_matches(er):
    assert er.find("AAPL IS GOING UP BUY BUY BUY NOW", "2020-06-01") == []
    assert ciks(er.find("AAPL IS GOING UP BUY BUY BUY NOW $AAPL", "2020-06-01")) == {"1": "cashtag"}


def test_multiword_company_name(er):
    m = er.find("Palantir Technologies reported a big beat", "2022-06-01")
    assert ciks(m) == {"2": "name"}


def test_single_word_name_needs_capitalisation_and_context(er):
    assert ciks(er.find("Apple is overvalued, I'm short the stock", "2020-06-01")) == {"1": "name"}
    assert er.find("I ate an apple and went home", "2020-06-01") == []
    assert er.find("Apple pie recipe", "2020-06-01") == []   # capitalised but no finance context


def test_names_are_not_matched_in_comments(er):
    ms = list(er_mod.mention_rows(er, "comment", {
        "id": "c1", "post_id": "p1", "created_utc": 1592000000, "body": "Apple is overvalued, short the stock",
        "subreddit": "x"}))
    assert ms == []


def test_post_mentions_merge_title_and_body(er):
    rows = list(er_mod.mention_rows(er, "post", {
        "id": "p1", "created_utc": 1592000000, "subreddit": "x",
        "title": "$AAPL thesis", "selftext": "AAPL has a moat. Also $NET."}))
    by = {r["cik"]: r for r in rows}
    assert by["1"]["in_title"] and by["1"]["n"] == 2 and by["1"]["method"] == "cashtag"
    assert not by["3"]["in_title"]


def test_blind_text_hides_tickers_and_names(er):
    text = "Palantir Technologies and PLTR: $PLTR beats; Palantir is cheap"
    ms = er.find(text, "2022-06-01")
    blind = er_mod.blind_text(text, ms)
    assert "pltr" not in blind.lower() and "palantir" not in blind.lower()
    assert blind.count("COMPANY_A") >= 3


def test_common_words_are_not_name_aliases():
    snaps = [row(D2, "DNOW", "11", "NOW Inc."), row(D2, "VALU", "12", "Value Line, Inc."),
             row(D2, "TGT", "13", "Target Corporation"), row(D2, "ONON", "14", "On Holding AG"),
             row(D2, "TUES", "15", "Tuesday Morning Corp"),
             row(D2, "NVDA", "16", "NVIDIA Corporation")]
    e = er_mod.EntityResolver(sm.SecurityMaster(snaps),
                              common_words=frozenset({"now", "value", "line", "target"}))
    text = "Now the Value of the stock Target rose on Tuesday, I bought shares of Nvidia"
    assert {m.cik for m in e.find(text, "2022-06-01")} == {"16"}


def test_a_leading_word_alone_is_not_a_company_alias():
    snaps = [row(D2, "AZRE", "21", "Azure Power Global Limited")]
    e = er_mod.EntityResolver(sm.SecurityMaster(snaps))
    assert e.find("Microsoft's Azure growth is strong, I own the stock", "2022-06-01") == []
    assert {m.cik for m in e.find("Azure Power Global reported earnings", "2022-06-01")} == {"21"}


def test_blind_tokens_keep_everyday_leading_words():
    import names
    assert names.blind_tokens("Palantir Technologies Inc") == ["palantir technologies", "palantir"]
    assert names.blind_tokens("Under Armour Inc") == ["under armour"]          # "under" is prose
    assert names.blind_tokens("Post Holdings Inc") == ["post"]                 # one core word: the name itself
    assert names.blind_tokens("Air Products & Chemicals Inc") == ["air products & chemicals"]
    assert names.blind_tokens("General Electric Co") == ["general electric"]   # generic first word
    assert names.blind_tokens("Apple Inc") == ["apple"]
    ms = [er_mod.Mention(cik="1", ticker="UA", name="Under Armour Inc", sec_type="CS", method="cashtag",
                         resolve_conf="stable", tickers={"UA", "UAA"})]
    out = er_mod.blind_text("UA is under pressure; Under Armour and $UAA fell", ms)
    assert out == "COMPANY_A is under pressure; COMPANY_A and COMPANY_A fell"
