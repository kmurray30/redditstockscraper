import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import thesis_prompt as tp  # noqa: E402

T0 = 1_400_000_000


def thread(**kw):
    base = dict(post_id="p1", cik="1", ticker="ACME", name="Acme Widgets Inc", created_utc=T0,
                subreddit="stocks", flair="DD", title="Why $ACME doubles: Acme's new widget",
                body="Acme Widgets ships a widget. ACME trades at 5x FCF. " * 20, url=None,
                is_self=True, author="op_user", votes=142, comments=[])
    base.update(kw)
    return tp.Thread(**base)


def comment(cid, parent, body, votes=0, author="x", created=T0 + 100):
    return {"comment_id": cid, "post_id": "p1", "parent_id": parent, "created_utc": created,
            "author": author, "body": body, "votes": votes}


def test_body_truncation_keeps_head_and_tail():
    body = "H" * 7000 + "M" * 5000 + "T" * 3000
    out = tp.truncate_body(body, head=6000, tail=2000)
    assert out.startswith("H" * 6000) and out.endswith("T" * 2000) and "omitted" in out
    assert tp.truncate_body("short") == "short"


def test_comments_are_pruned_by_votes_within_budget_and_labelled():
    cs = [comment("a", "t3_p1", "Low voted but long argument about margins " * 3, votes=2, author="al"),
          comment("b", "t3_p1", "Top comment: the filing says revenue fell 40%", votes=90, author="bo"),
          comment("c", "t3_p1", "this", votes=500),                     # too short: dropped
          comment("d", "t1_b", "Reply from OP: that is the old segment", votes=10, author="op_user"),
          comment("e", "t1_b", "Another reply with little content here", votes=1, author="al"),
          comment("f", "t1_b", "Third reply, should be cut by the reply cap", votes=0),
          comment("g", "t1_d", "Reply to the reply: fair, I misread it", votes=3, author="bo")]
    pruned = tp.prune_comments(cs, "p1", "op_user")
    order = [c["comment_id"] for c in pruned]
    assert order == ["b", "d", "g", "e", "a"]            # best-voted first, replies nested, f cut
    labels = {c["comment_id"]: c["label"] for c in pruned}
    assert labels["d"] == "OP" and labels["b"] == labels["g"] == "u1" and labels["e"] == labels["a"] == "u2"
    text = tp.render_comments(pruned, len(cs))
    assert "5 shown of 7" in text and "[90] u1: Top comment" in text and "  > [10] OP:" in text
    assert "    >> [3] u1:" in text and "this" not in text.split("\n")[1]
    # budget: many long comments -> only what fits
    many = [comment(f"m{i}", "t3_p1", "x" * 600, votes=100 - i) for i in range(30)]
    kept = tp.prune_comments(many, "p1", None)
    assert 0 < len(kept) < 30 and sum(len(c["body"]) + 20 for c in kept) <= tp.PRUNE["comment_budget"]


def test_identified_message_has_company_and_votes():
    t = thread(comments=[comment("a", "t3_p1", "Checked the 10-K, FCF is real", votes=12)])
    msg = tp.user_message(t, blinded=False)
    assert "COMPANY UNDER EVALUATION: Acme Widgets Inc (ACME)" in msg
    assert "AS-OF DATE: 2014-05-13" in msg and "POST VOTES: 142" in msg and "FLAIR: DD" in msg
    assert "[12] u1: Checked the 10-K" in msg
    req = tp.build_request(t, blinded=False)
    assert req.key == "p1_1" and req.schema is tp.SCHEMA and req.max_out == tp.MAX_OUT
    assert "why" in tp.build_request(t, blinded=False, why=True).schema["properties"]


def test_blinded_message_has_no_ticker_cashtag_or_name():
    t = thread(comments=[comment("a", "t3_p1", "ACME and Acme Widgets look cheap, $ACME to the moon",
                                 votes=3)],
               url="https://ir.acme.com/q", is_self=False)
    msg = tp.user_message(t, blinded=True)
    assert "COMPANY_A" in msg and "identity hidden" in msg
    assert tp.blind_leaks(msg, "ACME", "Acme Widgets Inc") == []
    assert "ir.acme.com" not in msg
    # the identified message does leak, so the check is not vacuous
    assert tp.blind_leaks(tp.user_message(t, blinded=False), "ACME", "Acme Widgets Inc")
    # the fixed header is template text, not a leak: ticker DATE, names led by "Under", "Post"
    header_only = "AS-OF DATE: 2015-01-01 (UTC).\nCOMPANY UNDER EVALUATION: COMPANY_A\nSUBREDDIT: r/stocks   POST VOTES: 3\n\nTITLE: x\n\nBODY:\ny\n"
    assert tp.blind_leaks(header_only, "DATE", "Date Inc") == []
    assert tp.blind_leaks(header_only, "UA", "Under Armour Inc") == []
    assert tp.blind_leaks(header_only, "PPS", "Post Properties Inc") == []
    assert tp.blind_leaks(header_only + "under armour makes shoes", "UA", "Under Armour Inc") == ["under armour"]
    assert tp.blind_leaks(header_only + "Palantir is cheap", "PLTR", "Palantir Technologies Inc") == ["palantir"]


def test_prompt_version_changes_with_prompt_text(monkeypatch, tmp_path):
    v1 = tp.prompt_version()
    f = tmp_path / "thesis_v1.md"
    f.write_text(tp.prompt_text() + "\nextra rule")
    monkeypatch.setattr(tp, "PROMPT_FILE", f)
    assert tp.prompt_version() != v1 and tp.prompt_version().endswith(f".b{tp.BUILDER_VERSION}")


def test_no_usernames_in_the_message():
    t = thread(author="secret_op", comments=[comment("a", "t3_p1", "A substantive comment here",
                                                      author="secret_commenter", votes=1)])
    msg = tp.user_message(t, blinded=False)
    assert "secret_op" not in msg and "secret_commenter" not in msg


def test_jev_questions_cover_the_schema_and_decode_to_it():
    q = tp.jev_questions()
    assert set(q) == {"is_thesis", "stance", "horizon", "n_independent_voices", *tp.JEV_SCORES}
    assert q["horizon"]["type"] == "choice" and set(q["horizon"]["criteria"]) == set(tp.SCHEMA["properties"]["horizon"]["enum"])
    assert all(2 <= len(x["criteria"]) <= 10 for x in q.values() if x["type"] == "score")
    assert len(q["n_independent_voices"]["criteria"]) == 10 and len(q["stance"]["criteria"]) == 5
    answers = {k: {"type": "score", "score": 3.0, "confidence": 0.6} for k in tp.JEV_SCORES}
    answers["stance"] = {"type": "score", "score": 4.0, "confidence": 0.9}
    answers["n_independent_voices"] = {"type": "score", "score": 2.4, "confidence": 0.5}
    answers["is_thesis"] = {"type": "noul", "noul": 0.8}
    answers["horizon"] = {"type": "choice", "choice": "years", "probabilities": {"years": 0.7, "months": 0.3}}
    g = tp.decode_jev(answers)
    assert g["is_thesis"] is True and g["stance"] == 1.0 and g["horizon"] == "years"
    assert g["argument_quality"] == 75 and g["comments"]["independent_support"] == 75
    assert g["comments"]["n_independent_voices"] == 2 and g["_jev"]["is_thesis_p"] == 0.8
    assert g["confidence"] == pytest.approx(round((0.6 * 10 + 0.9 + 0.5) / 12, 3))
    assert set(tp.SCHEMA["required"]) <= set(g)
    from thesis_scorer import Params, thread_quality
    assert 0 < thread_quality(g, Params()) <= 100
    answers["stance"]["score"] = 0.0
    assert tp.decode_jev(answers)["stance"] == -1.0


def test_build_request_backends_and_prompt_version(thread=None):
    t = tp.Thread(post_id="p", cik="1", ticker="ACM", name="Acme", created_utc=1_400_000_000,
                  subreddit="stocks", flair=None, title="t", body="b", url=None, is_self=True,
                  author="op", votes=3, comments=[])
    chat, jev = tp.build_request(t, blinded=False, why=True), tp.build_request(t, blinded=False, why=True, backend="jev")
    assert chat.questions is None and "why" in chat.schema["properties"]
    assert jev.questions == tp.jev_questions() and jev.decode is tp.decode_jev and jev.max_out == 0
    assert "why" not in jev.schema["properties"] and chat.sha != jev.sha and chat.key == jev.key
    assert tp.prompt_version().startswith("thesis_v1.") and tp.prompt_version("gpt-5-nano") == tp.prompt_version()
    assert tp.prompt_version("jev-1.13.0") == tp.prompt_version("typesafe/jev-1.13") != tp.prompt_version()
    assert tp.prompt_version("jev-1.13.0").startswith(tp.prompt_version() + ".jev")


def test_merged_cross_post_reads_as_one_thread():
    comments = [{"comment_id": "c1", "parent_id": "t3_p1", "post_id": "p1", "created_utc": 5, "author": "x",
                 "body": "top-level under the primary, long enough to keep", "votes": 3},
                {"comment_id": "c2", "parent_id": "t3_p2", "post_id": "p2", "created_utc": 6, "author": "y",
                 "body": "top-level under the copy, long enough to keep too", "votes": 9},
                {"comment_id": "c3", "parent_id": "t1_c2", "post_id": "p2", "created_utc": 7, "author": "z",
                 "body": "a reply under the copy's comment, long enough", "votes": 1}]
    merged = tp.merge_comments(comments, "p1", ["p1", "p2"])
    assert [c["parent_id"] for c in merged] == ["t3_p1", "t3_p1", "t1_c2"]
    assert tp.merge_comments(comments, "p1", ["p1"]) == comments
    post = {"post_id": "p1", "cik": "1", "ticker": "T1", "created_utc": 1_400_000_000, "subreddit": "stocks",
            "title": "Big DD", "selftext": "body " * 100, "is_self": True, "author": "a", "votes": 10}
    t = tp.Thread.from_view(post, merged, "T One Inc", votes=15, also_in=["stocks", "investing"])
    assert t.votes == 15 and t.also_in == ("investing",)
    msg = tp.user_message(t, blinded=False)
    assert "SUBREDDIT: r/stocks (also posted in r/investing)   POST VOTES: 15" in msg
    assert "[9] u1: top-level under the copy" in msg and "> [1] u2: a reply" in msg
    plain = tp.Thread.from_view(post, comments[:1], "T One Inc")
    assert "also posted" not in tp.user_message(plain, blinded=False) and plain.votes == 10
