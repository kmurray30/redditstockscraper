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
