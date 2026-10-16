import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from reddit_filter import (  # noqa: E402
    MentionCounter,
    TickerMatcher,
    is_relevant_post,
    load_tickers,
    slim,
)

M = TickerMatcher({"NET", "AAPL", "TSLA", "NOW", "ALL", "F", "IT", "PLTR"})


def test_cashtags_always_match_even_unknown_or_lowercase():
    assert M.match("loading up on $net and $zzzz, also $f") == {"NET", "ZZZZ", "F"}


def test_cashtag_currency_and_dollar_amounts_ignored():
    assert M.match("costs $USD 5, made $500 today, $5.20") == set()


def test_bare_tokens_need_list_and_not_stoplist():
    assert M.match("NET looks cheap vs AAPL") == {"NET", "AAPL"}
    # lowercase english, stoplisted uppercase words, unknown uppercase
    assert M.match("net income grew. ALL IT NOW CEO said XYZQ") == set()
    # stoplisted ticker still matches as a cashtag
    assert M.match("$NOW is a great business") == {"NOW"}


def test_single_letter_only_via_cashtag():
    assert M.match("F is a car company, I think") == set()
    assert M.match("$F") == {"F"}


def post(**kw):
    base = {"id": "p1", "title": "", "selftext": "", "created_utc": 1600000000}
    base.update(kw)
    return base


def test_megathreads_dropped_even_with_tickers():
    keep, reason, _ = is_relevant_post(post(title="Daily Discussion Thread $TSLA"), M)
    assert (keep, reason) == (False, "megathread")
    keep, reason, _ = is_relevant_post(post(title="My PLTR thesis", stickied=True), M)
    assert (keep, reason) == (False, "megathread")


def test_ticker_post_kept():
    keep, reason, tickers = is_relevant_post(post(title="Why PLTR is undervalued"), M)
    assert keep and reason == "ticker" and tickers == {"PLTR"}


def test_removed_body_needs_title_ticker():
    keep, reason, _ = is_relevant_post(post(title="hello", selftext="[removed]"), M)
    assert (keep, reason) == (False, "removed")
    keep, reason, _ = is_relevant_post(post(title="$TSLA", selftext="[removed]"), M)
    assert keep and reason == "ticker"


def test_flair_and_longform_rules():
    keep, reason, _ = is_relevant_post(post(title="a semiconductor deep dive", link_flair_text="DD"), M)
    assert keep and reason == "flair"
    body = ("The company has a durable moat and expanding gross margin. " * 20) + "Valuation looks fine."
    keep, reason, _ = is_relevant_post(post(title="thoughts on a niche business", selftext=body), M)
    assert keep and reason == "longform"


def test_noise_dropped():
    assert is_relevant_post(post(title="look at this", url="https://i.redd.it/x.png"), M)[1] == "no_text"
    assert is_relevant_post(post(title="lol", selftext="to the moon boys"), M)[1] == "no_signal"


def test_slim_keeps_only_listed_fields():
    out = slim({"id": "a", "body": "x", "junk": 1}, ["id", "body"], {"NET"})
    assert out == {"id": "a", "body": "x", "matched_tickers": ["NET"]}


def test_mention_counter_csv(tmp_path):
    c = MentionCounter()
    c.add(1600000000, "stocks", {"NET", "AAPL"}, "post")
    c.add(1600000100, "stocks", {"NET"}, "comment")
    c.add(1600000200, "stocks", {"NET"}, "comment")
    c.add(1600000300, "stocks", set(), "comment")
    c.write_csv(tmp_path / "m.csv")
    lines = (tmp_path / "m.csv").read_text().splitlines()
    assert lines[0] == "date,subreddit,ticker,source,count"
    assert "2020-09-13,stocks,NET,comment,2" in lines
    assert len(lines) == 4


def test_load_tickers_from_file(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("# comment\naapl\nNET\n\n")
    assert load_tickers(str(f), tmp_path) == {"AAPL", "NET"}


def test_discussed_link_posts_kept_without_a_ticker():
    link = post(title="Berkshire's 2019 shareholder letter", is_self=False,
                url="https://berkshirehathaway.com/letters/2019ltr.pdf", num_comments=5)
    keep, reason, _ = is_relevant_post(link, M)
    assert (keep, reason) == (True, "discussed_link")
    quiet = dict(link, num_comments=4)
    assert is_relevant_post(quiet, M)[:2] == (False, "no_text")
    # a text post with the same comment count is not rescued by the link rule
    selfpost = post(title="what do you think", selftext="hmm", is_self=True, num_comments=50)
    assert is_relevant_post(selfpost, M)[:2] == (False, "no_signal")
    # threshold is configurable
    assert is_relevant_post(quiet, M, min_link_comments=3)[1] == "discussed_link"


def test_removed_link_posts_and_megathreads_are_still_dropped():
    removed = post(title="great letter", is_self=False, num_comments=99, removed_by_category="moderator")
    assert is_relevant_post(removed, M)[:2] == (False, "removed")
    mega = post(title="Daily Discussion Thread", is_self=False, num_comments=999)
    assert is_relevant_post(mega, M)[:2] == (False, "megathread")


def test_is_empty_comment():
    from reddit_filter import is_empty_comment
    assert is_empty_comment({"body": "[removed]"}) and is_empty_comment({"body": " [Deleted] "})
    assert is_empty_comment({"body": ""}) and is_empty_comment({})
    assert not is_empty_comment({"body": "NET looks cheap"})


def test_finance_abbreviations_roman_numerals_and_codes_are_not_tickers():
    m = TickerMatcher({"VC", "IP", "IR", "SG", "NPV", "FT", "SA", "BV", "CF", "AR", "IQ", "III",
                       "CA", "NET", "GM", "GE"})
    text = "VC and IP stuff, IR page, SG&A, NPV vs BV, CF, AR, IQ, FT, SA, Q4 III, CA"
    assert m.match(text) == set()
    # genuine company usage still works bare, and every one still works as a cashtag
    assert m.match("GM and GE reported; NET too") == {"GM", "GE", "NET"}
    assert m.match("$VC $IP $III") == {"VC", "IP", "III"}


def test_shouting_text_only_matches_cashtags():
    m = TickerMatcher({"NET", "AAPL"})
    assert m.match("THE NEXT BIG THING IS NET AND AAPL TO THE MOON") == set()
    assert m.match("THE NEXT BIG THING IS $NET TO THE MOON") == {"NET"}
    assert m.match("Short ALL CAPS NET is fine") == {"NET"}  # too short to count as shouting


def test_extra_stoplist_file(tmp_path):
    from reddit_filter import load_stoplist, stoplist_fingerprint
    f = tmp_path / "stop.txt"
    f.write_text("# my words\nnet\n\nAAPL\n")
    extra = load_stoplist(str(f))
    assert extra == frozenset({"NET", "AAPL"})
    m = TickerMatcher({"NET", "AAPL", "TSLA"}, extra)
    assert m.match("NET AAPL TSLA") == {"TSLA"}
    assert m.match("$NET") == {"NET"}
    assert stoplist_fingerprint(extra) != stoplist_fingerprint(frozenset()) == ""


def test_slim_unescapes_html_entities():
    out = slim({"id": "a", "title": "$TAL &amp; $EDU &gt; peers", "selftext": "R&amp;D",
                "score": 1}, ["id", "title", "selftext", "score"])
    assert out["title"] == "$TAL & $EDU > peers" and out["selftext"] == "R&D" and out["score"] == 1


def test_personal_finance_words_from_r_investing_are_not_tickers():
    m = TickerMatcher({"CD", "TD", "LOT", "YOU", "LINK", "NOTE", "IEX", "KEY", "FDIC", "HSA",
                       "ROTH", "AAPL"})
    text = "Best CD rates? I use TD, a LOT, YOU should NOTE the KEY LINK on the FDIC HSA ROTH page"
    assert m.match(text) == set()
    assert m.match("$CD and $TD still work as cashtags; AAPL bare too") == {"CD", "TD", "AAPL"}


def test_recurring_advice_threads_are_megathreads():
    for title in ("It's moronic Monday, the Wednesday edition, your chance to ask questions",
                  "Daily Advice Thread - ask anything", "Newbie Wednesday", "Simple Questions",
                  "Sunday Discussion Thread"):
        assert is_relevant_post(post(title=title + " $AAPL"), M)[:2] == (False, "megathread"), title
    # a normal question with a ticker is untouched
    assert is_relevant_post(post(title="Is AAPL a buy?"), TickerMatcher({"AAPL"}))[0] is True
