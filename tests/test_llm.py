import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import llm  # noqa: E402

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"q": {"type": "integer", "minimum": 0, "maximum": 100},
                         "ok": {"type": "boolean"},
                         "h": {"type": "string", "enum": ["a", "b"]},
                         "sub": {"type": "object", "additionalProperties": False,
                                 "properties": {"c": {"type": "number", "minimum": 0, "maximum": 1}},
                                 "required": ["c"]}},
          "required": ["q", "ok", "h", "sub"]}


def req(i, text="hello"):
    return llm.Request(key=f"k{i}", system="sys", user=f"{text} {i}", schema=SCHEMA, max_out=100)


def cfg(**kw):
    base = dict(model="fake", price_in=1.0, price_out=2.0, cap_usd=5.0, workers=2)
    base.update(kw)
    return llm.Config(**base)


def test_config_from_env_reads_keys_from_env_file_and_never_shows_them(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-secret\nLLM_MODEL=gpt-5-nano\n")
    c = llm.Config.from_env(env={}, env_path=tmp_path / ".env")
    assert c.api_key == "sk-secret" and "sk-secret" not in repr(c)
    assert (c.price_in, c.price_out) == llm.PRICES["gpt-5-nano"]
    assert c.extra == {"reasoning_effort": "minimal"}
    with pytest.raises(SystemExit, match="LLM_MODEL"):
        llm.Config.from_env(env={}, env_path=tmp_path / "none")
    with pytest.raises(SystemExit, match="API key"):
        llm.Config.from_env(env={"LLM_MODEL": "gpt-5-nano"}, env_path=tmp_path / "none")
    with pytest.raises(SystemExit, match="LLM_PRICE_IN"):
        llm.Config.from_env(env={"LLM_MODEL": "mystery-9", "LLM_API_KEY": "x"},
                            env_path=tmp_path / "none")
    c = llm.Config.from_env(env={"LLM_MODEL": "openai/gpt-5-nano", "OPENROUTER_API_KEY": "k",
                                 "LLM_BASE_URL": "https://openrouter.ai/api/v1"},
                            env_path=tmp_path / "none")
    assert c.price_in == 0.05 and c.base_url.startswith("https://openrouter")
    assert llm.price_for("typesafe/jev-1.13", {}) == (0.042, 0.0)


def test_fake_client_is_deterministic_and_schema_valid():
    a, _ = llm.FakeClient().complete(req(1))
    b, _ = llm.FakeClient().complete(req(1))
    c, _ = llm.FakeClient().complete(req(2))
    assert a == b and a != c
    assert set(a) == {"q", "ok", "h", "sub"} and 0 <= a["q"] <= 100 and a["h"] in ("a", "b")
    assert 0 <= a["sub"]["c"] <= 1


def test_run_caches_and_never_pays_twice(tmp_path):
    cache, spend = llm.Cache(tmp_path / "c"), llm.Spend(tmp_path / "spend.jsonl")
    client = llm.FakeClient()
    out = llm.run([req(1), req(2)], cfg(), cache, spend, client, log=lambda *_: None)
    assert set(out) == {"k1", "k2"} and client.calls == 2
    assert spend.total > 0 and len((tmp_path / "spend.jsonl").read_text().splitlines()) == 2
    # second run: everything cached, no calls, no spend
    before = spend.total
    out2 = llm.run([req(1), req(2)], cfg(), cache, llm.Spend(tmp_path / "spend.jsonl"), client,
                   log=lambda *_: None)
    assert out2 == out and client.calls == 2 and llm.Spend(tmp_path / "spend.jsonl").total == before
    # a changed request text is a cache miss
    llm.run([req(1, "changed")], cfg(), cache, spend, client, log=lambda *_: None)
    assert client.calls == 3
    rec = json.loads((tmp_path / "c" / "k1.json").read_text())
    assert rec["user"] == "changed 1" and rec["cost"] > 0


def test_cap_refuses_before_the_first_call(tmp_path):
    cache, spend = llm.Cache(tmp_path / "c"), llm.Spend(tmp_path / "spend.jsonl")
    client = llm.FakeClient()
    reqs = [req(i) for i in range(50)]
    with pytest.raises(llm.SpendCapError, match="refused"):
        llm.run(reqs, cfg(price_in=1e6, cap_usd=0.01), cache, spend, client, log=lambda *_: None)
    assert client.calls == 0
    # dry run never calls or refuses
    llm.run(reqs, cfg(price_in=1e6, cap_usd=0.01), cache, spend, client, dry_run=True,
            log=lambda *_: None)
    assert client.calls == 0


def test_cap_stops_a_run_in_progress(tmp_path):
    cache, spend = llm.Cache(tmp_path / "c"), llm.Spend(tmp_path / "spend.jsonl")
    client = llm.FakeClient()
    c = cfg(price_in=0.0, price_out=1e5, cap_usd=0.5, workers=1)   # $0.01 per call, estimate 0.6*...
    reqs = [req(i) for i in range(200)]
    # estimate = 200 * 60 * 1e5 / 1e6 = 1.2 > cap -> refused; so loosen the estimate fraction
    out = llm.run(reqs, c, cache, spend, client, log=lambda *_: None, out_fraction=0.0)
    assert 0 < len(out) < 200 and client.calls < 200
    assert spend.total >= 0.5


def test_http_client_retries_then_raises(monkeypatch):
    import urllib.error
    calls = []

    class Boom(llm.Client):
        def _post(self, url, data, headers):
            calls.append(json.loads(data))
            raise urllib.error.HTTPError(url, 500, "boom", {}, None)

    c = Boom(cfg(model="gpt-5-nano", api_key="sk-secret", base_url="https://x.test/v1"), retries=2,
             sleep=lambda s: None)
    with pytest.raises(llm.LLMError) as e:
        c.complete(req(1))
    assert len(calls) == 3 and "sk-secret" not in str(e.value)
    body = calls[0]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_completion_tokens"] == 100 and body["messages"][1]["content"] == "hello 1"


def test_http_client_parses_a_good_response():
    class Ok(llm.Client):
        def _post(self, url, data, headers):
            assert headers["authorization"] == "Bearer k"
            return {"choices": [{"message": {"content": json.dumps({"q": 5})}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3}}

    result, usage = Ok(cfg(model="gpt-5-nano", api_key="k")).complete(req(1))
    assert result == {"q": 5} and usage["completion_tokens"] == 3


JEV_Q = {"ok": {"type": "noul", "instructions": "yes?", "criteria": {"true": "t", "false": "f"}},
         "lvl": {"type": "score", "instructions": "how much?", "criteria": ["none", "some", "lots"]},
         "pick": {"type": "choice", "instructions": "which?", "criteria": {"a": "A", "b": "B"}}}


def jev_req(i=1):
    return llm.Request(key=f"j{i}", system="rules", user=f"text {i}", schema=SCHEMA, max_out=0,
                       questions=JEV_Q, decode=lambda ans: {"q": int(ans["lvl"]["score"] * 50),
                                                            "ok": ans["ok"]["noul"] >= 0.5,
                                                            "h": ans["pick"]["choice"], "sub": {"c": 0.5}})


def test_jev_config_routes_by_key_and_normalises_the_model_id(tmp_path):
    none = tmp_path / "none"
    c = llm.Config.from_env(env={"LLM_MODEL": "typesafe/jev-1.13", "JEV_API_KEY": "k"}, env_path=none)
    assert c.backend == "jev" and c.model == "jev-1.13.0" and c.base_url == llm.JEV_DIRECT_URL
    assert (c.price_in, c.price_out) == (0.042, 0.0) and c.extra == {}
    c = llm.Config.from_env(env={"LLM_MODEL": "jev-1.13.0", "OPENROUTER_API_KEY": "k"}, env_path=none)
    assert c.model == "typesafe/jev-1.13" and c.base_url == llm.JEV_OPENROUTER_URL
    c = llm.Config.from_env(env={"LLM_MODEL": "jev-1.13.0", "TYPESAFE_API_KEY": "k",
                                 "LLM_BASE_URL": "https://x/y"}, env_path=none)
    assert c.base_url == "https://x/y" and c.api_key == "k"
    with pytest.raises(SystemExit, match="Jev"):
        llm.Config.from_env(env={"LLM_MODEL": "jev-1.13.0", "OPENAI_API_KEY": "k"}, env_path=none)
    assert isinstance(llm.make_client(c), llm.JevClient)
    assert llm.Request(key="a", system="s", user="u", schema=SCHEMA).sha != jev_req().sha
    assert jev_req().est_in_tokens > llm.Request(key="a", system="rules", user="text 1", schema=SCHEMA).est_in_tokens


def test_jev_client_posts_state_and_questions_and_decodes_answers(monkeypatch):
    c = llm.JevClient(cfg(model="jev-1.13.0", backend="jev", base_url=llm.JEV_DIRECT_URL, api_key="k"))
    seen = {}

    def fake_post(url, data, headers):
        seen["url"], seen["body"], seen["auth"] = url, json.loads(data), headers["authorization"]
        return {"model": "jev-1.13.0",
                "answers": {"ok": {"type": "noul", "noul": 0.9},
                            "lvl": {"type": "score", "score": 1.5, "legend": {"0": "none"},
                                    "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}, "confidence": 0.7},
                            "pick": {"type": "choice", "choice": "b", "probabilities": {"a": 0.2, "b": 0.8},
                                     "confidence": 0.8}},
                "usage": {"input_tokens": 1200, "output_tokens": 0, "cost": 0.0000504}}
    monkeypatch.setattr(c, "_post", fake_post)
    result, usage = c.complete(jev_req())
    assert seen["url"] == llm.JEV_DIRECT_URL and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "jev-1.13.0" and seen["body"]["questions"] == JEV_Q
    assert seen["body"]["state"] == {"rules": "rules", "discussion": "text 1"}
    assert result == {"q": 75, "ok": True, "h": "b", "sub": {"c": 0.5}}
    assert usage["prompt_tokens"] == 1200 and usage["cost"] == 0.0000504
    with pytest.raises(llm.LLMError, match="no Jev questions"):
        c.complete(req(1))


def test_jev_answers_are_validated():
    good = {"ok": {"type": "noul", "noul": 0.2}, "lvl": {"type": "score", "score": 2.0},
            "pick": {"type": "choice", "choice": "a"}}
    assert llm.validate_jev_answers(good, JEV_Q) is good
    for bad, msg in [({**good, "lvl": {"type": "score", "score": 2.5}}, "out of range"),
                     ({**good, "ok": {"type": "noul", "noul": 1.5}}, "probability"),
                     ({**good, "pick": {"type": "choice", "choice": "zz"}}, "not an option"),
                     ({k: v for k, v in good.items() if k != "pick"}, "missing"),
                     ({**good, "pick": {"type": "choice", "choice": "a", "probabilities": {"a": float("nan")}}},
                      "malformed"),
                     (None, "no answers")]:
        with pytest.raises(llm.LLMError, match=msg):
            llm.validate_jev_answers(bad, JEV_Q)


def test_fake_backend_answers_jev_requests_and_run_books_reported_cost(tmp_path):
    r1, _ = llm.FakeClient().complete(jev_req(1))
    r2, _ = llm.FakeClient().complete(jev_req(1))
    assert r1 == r2 and set(r1) == {"q", "ok", "h", "sub"} and r1["h"] in ("a", "b") and 0 <= r1["q"] <= 100
    cache, spend = llm.Cache(tmp_path / "c"), llm.Spend(tmp_path / "s.jsonl")

    class Reported:
        def complete(self, r):
            return {"q": 1, "ok": True, "h": "a", "sub": {"c": 0.1}}, {"prompt_tokens": 1000, "completion_tokens": 0,
                                                                       "cost": 0.123}
    llm.run([jev_req(7)], cfg(model="jev-1.13.0", backend="jev"), cache, spend, client=Reported(), log=lambda *_: None)
    assert spend.total == pytest.approx(0.123)        # the provider's figure, not tokens x price
