"""OpenAI-compatible chat client with structured JSON output, an on-disk cache and a spend cap.

Used by the thesis scorer. Nothing here knows about Reddit; it moves requests to a model
and results to disk, and it refuses to spend more than it is allowed to.

Configuration (environment, or ``.env`` for the keys; see ``Config.from_env``):

    LLM_MODEL            e.g. gpt-5-nano (or openai/gpt-5-nano via OpenRouter); ``jev-1.13.0`` for
                         TypeSafe AI's Jev, which is not a chat model (see "Jev" below);
                         ``fake`` is a free deterministic backend for tests and dry runs
    LLM_BASE_URL         default https://api.openai.com/v1 (OpenRouter: https://openrouter.ai/api/v1);
                         for Jev the full decisions endpoint (default api.typesafe.ai/v1/systemone)
    OPENAI_API_KEY | OPENROUTER_API_KEY | LLM_API_KEY   never printed, never cached
    JEV_API_KEY | TYPESAFE_API_KEY   TypeSafe's own key (direct endpoint); OPENROUTER_API_KEY also
                         reaches Jev, through OpenRouter's alpha decisions endpoint
    LLM_SPEND_CAP_USD    hard cap over the lifetime log ``data/llm/spend.jsonl`` (default 5)
    LLM_PRICE_IN / LLM_PRICE_OUT   USD per 1M tokens; required for a model not in PRICES
    LLM_WORKERS          parallel requests (default 8)
    LLM_EXTRA_JSON       extra request-body fields, e.g. {"reasoning_effort": "minimal"}

Jev (``Config.backend == "jev"``): Jev answers typed questions over a block of text instead
of writing text: ``choice`` (one of named options, with probabilities), ``noul`` (a yes
probability) and ``score`` (a probability-weighted index over 2-10 ordered rubric levels).
A ``Request`` for Jev carries ``questions`` in that form and a ``decode`` callable that turns
the answers into the same JSON shape a chat model would have returned, so caches, collect
and scoring do not care which backend graded a thread. The body is
``{"model", "state": {"rules": system, "discussion": user}, "questions"}``; the response is
``{"model", "answers": {id: ...}, "usage": {"input_tokens", "output_tokens", "cost"?}}``.
Jev's own reported ``cost`` is booked when present. Contract per the public references of
September 2026 (the direct route is unverified from the build container; the first live call
is the test). Output tokens are free, so a Jev run costs about a third of GPT-5 nano.

Spend discipline: before a run, the cost of the *uncached* requests is estimated from their
token counts at the model's price and the run is refused if the lifetime spend plus that
estimate exceeds the cap. During a run every response's real usage is appended to the spend
log, and the run stops the moment the log crosses the cap. A result is cached by request
hash, so a rerun never pays twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CAP = 5.0
KEY_NAMES = ("LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
JEV_DIRECT_URL = "https://api.typesafe.ai/v1/systemone"
JEV_OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_DIRECT_KEYS = ("JEV_API_KEY", "TYPESAFE_API_KEY")
JEV_MODEL_IDS = {"direct": "jev-1.13.0", "openrouter": "typesafe/jev-1.13"}

# USD per 1M tokens (input, output). From third-party price trackers in October 2026, because the
# provider pages were unreachable from the build container: verify before a large run. Output
# includes reasoning tokens on reasoning models, which is why ``reasoning_effort`` matters.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "jev-1.13": (0.042, 0.0),         # TypeSafe AI Jev: output is free (it returns typed decisions)
    "jev-1.13.0": (0.042, 0.0),
    "fake": (0.0, 0.0),
}


class LLMError(RuntimeError):
    pass


class SpendCapError(LLMError):
    pass


def _read_env_file(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            name = name.strip().removeprefix("export ").strip()
            if name and value.strip() and not name.startswith("#"):
                out[name] = value.strip().strip("'\"")
    return out


def is_jev(model: str) -> bool:
    return "jev" in model.lower().split("/")[-1]


def price_for(model: str, env: dict) -> tuple[float, float]:
    if env.get("LLM_PRICE_IN") and env.get("LLM_PRICE_OUT"):
        return float(env["LLM_PRICE_IN"]), float(env["LLM_PRICE_OUT"])
    for key in (model, model.split("/")[-1], model.split("/")[-1].rsplit("-", 1)[0]):
        if key in PRICES:
            return PRICES[key]
    raise SystemExit(f"no price known for model {model!r}: set LLM_PRICE_IN and LLM_PRICE_OUT "
                     "(USD per 1M tokens) after checking the provider's price page")


@dataclass
class Config:
    model: str
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = field(default=None, repr=False)
    price_in: float = 0.0
    price_out: float = 0.0
    cap_usd: float = DEFAULT_CAP
    workers: int = 8
    timeout: float = 120.0
    extra: dict = field(default_factory=dict)
    backend: str = "chat"            # chat | jev

    @classmethod
    def from_env(cls, env: dict | None = None, env_path: Path | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        for k, v in _read_env_file(env_path or ROOT / ".env").items():
            env.setdefault(k, v)
        model = env.get("LLM_MODEL")
        if not model:
            raise SystemExit("LLM_MODEL is not set (e.g. gpt-5-nano, jev-1.13.0, or fake for a dry run)")
        if is_jev(model):
            return cls._jev_from_env(model, env)
        key = next((env[k] for k in KEY_NAMES if env.get(k)), None)
        if model != "fake" and not key:
            raise SystemExit("no API key: set OPENAI_API_KEY (or OPENROUTER_API_KEY / LLM_API_KEY)")
        pin, pout = price_for(model, env)
        extra = json.loads(env["LLM_EXTRA_JSON"]) if env.get("LLM_EXTRA_JSON") else {}
        if model.split("/")[-1].startswith("gpt-5") and "reasoning_effort" not in extra:
            extra["reasoning_effort"] = "minimal"   # reasoning tokens are billed as output
        return cls(model=model, base_url=env.get("LLM_BASE_URL") or DEFAULT_BASE_URL, api_key=key,
                   price_in=pin, price_out=pout,
                   cap_usd=float(env.get("LLM_SPEND_CAP_USD") or DEFAULT_CAP),
                   workers=int(env.get("LLM_WORKERS") or 8), extra=extra)

    @classmethod
    def _jev_from_env(cls, model: str, env: dict) -> "Config":
        """Jev: TypeSafe's own key goes to the direct endpoint, an OpenRouter key to OpenRouter's
        decisions endpoint; the model id is normalised to the route's spelling."""
        direct = next((env[k] for k in JEV_DIRECT_KEYS if env.get(k)), None)
        if direct:
            key, route, url = direct, "direct", JEV_DIRECT_URL
        elif env.get("OPENROUTER_API_KEY"):
            key, route, url = env["OPENROUTER_API_KEY"], "openrouter", JEV_OPENROUTER_URL
        else:
            raise SystemExit("no API key for Jev: set JEV_API_KEY (TypeSafe) or OPENROUTER_API_KEY")
        if model in JEV_MODEL_IDS.values():
            model = JEV_MODEL_IDS[route]
        pin, pout = price_for(model, env)
        return cls(model=model, base_url=env.get("LLM_BASE_URL") or url, api_key=key,
                   price_in=pin, price_out=pout,
                   cap_usd=float(env.get("LLM_SPEND_CAP_USD") or DEFAULT_CAP),
                   workers=int(env.get("LLM_WORKERS") or 8), backend="jev")

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1e6


@dataclass(frozen=True)
class Request:
    key: str                 # cache file name, e.g. "<post_id>_<cik>"
    system: str
    user: str
    schema: dict
    schema_name: str = "result"
    max_out: int = 300
    # Jev form of the same request: typed questions over {rules: system, discussion: user}, and
    # a decoder from Jev's answers to the ``schema`` shape. Neither takes part in equality.
    questions: dict | None = field(default=None, compare=False, hash=False)
    decode: object = field(default=None, compare=False, hash=False, repr=False)

    @property
    def sha(self) -> str:
        blob = json.dumps({"s": self.system, "u": self.user, "j": self.schema,
                           **({"q": self.questions} if self.questions else {})}, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()

    @property
    def est_in_tokens(self) -> int:
        q = len(json.dumps(self.questions)) if self.questions else 0
        return (len(self.system) + len(self.user) + q) // 4 + 40


# ------------------------------------------------------------------ cache and spend log

class Cache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, req: Request) -> dict | None:
        p = self.path(req.key)
        if not p.exists():
            return None
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            return None
        return rec if rec.get("sha") == req.sha else None

    def put(self, req: Request, result: dict, usage: dict, cost: float, model: str) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        rec = {"key": req.key, "sha": req.sha, "model": model, "ts": _now(), "usage": usage,
               "cost": cost, "result": result, "user": req.user}
        tmp = self.path(req.key).with_suffix(".tmp")
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, self.path(req.key))
        return rec


class Spend:
    """Lifetime spend log; thread-safe."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.total = self._read_total()

    def _read_total(self) -> float:
        if not self.path.exists():
            return 0.0
        tot = 0.0
        for line in self.path.read_text().splitlines():
            try:
                tot += float(json.loads(line).get("cost", 0))
            except ValueError:
                continue
        return tot

    def add(self, model: str, usage: dict, cost: float, key: str = "") -> float:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({"ts": _now(), "model": model, "key": key, "cost": round(cost, 8),
                                    "prompt_tokens": usage.get("prompt_tokens"),
                                    "completion_tokens": usage.get("completion_tokens")}) + "\n")
            self.total += cost
            return self.total


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ clients

class Client:
    """One structured chat completion per call. Raises LLMError on a non-retryable failure."""

    RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

    def __init__(self, cfg: Config, retries: int = 5, sleep=time.sleep):
        self.cfg, self.retries, self._sleep = cfg, retries, sleep

    def complete(self, req: Request) -> tuple[dict, dict]:
        body = {"model": self.cfg.model,
                "messages": [{"role": "system", "content": req.system},
                             {"role": "user", "content": req.user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": req.schema_name, "schema": req.schema,
                                                    "strict": True}},
                "max_completion_tokens": req.max_out, **self.cfg.extra}
        resp = self.post_json(self.cfg.base_url.rstrip("/") + "/chat/completions", body)
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if msg.get("refusal"):
            raise LLMError(f"model refused: {msg['refusal'][:200]}")
        content = msg.get("content")
        if not content:
            raise LLMError(f"empty response (finish_reason={choice.get('finish_reason')!r}); "
                           "raise max_out or lower reasoning_effort")
        try:
            result = json.loads(content)
        except ValueError:
            raise LLMError(f"response is not JSON: {content[:200]!r}") from None
        return result, resp.get("usage") or {}

    def post_json(self, url: str, body: dict) -> dict:
        """POST with bounded retries on transient HTTP and network failures."""
        data = json.dumps(body).encode()
        headers = {"content-type": "application/json", "authorization": f"Bearer {self.cfg.api_key}"}
        last = None
        for attempt in range(self.retries + 1):
            try:
                return self._post(url, data, headers)
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:500]
                if e.code in self.RETRY_STATUS and attempt < self.retries:
                    last = f"HTTP {e.code}: {text}"
                else:
                    raise LLMError(f"HTTP {e.code} from {url}: {text}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = f"{type(e).__name__}: {e}"
                if attempt >= self.retries:
                    raise LLMError(f"request failed after {self.retries + 1} tries: {last}") from None
            self._sleep(min(2 ** attempt, 30) + random.random())
        raise LLMError(last or "unknown failure")  # pragma: no cover - loop returns or raises

    def _post(self, url: str, data: bytes, headers: dict) -> dict:
        r = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(r, timeout=self.cfg.timeout) as f:
            return json.loads(f.read().decode())


class JevClient(Client):
    """TypeSafe AI Jev: one decisions call per request, answers validated and decoded."""

    def complete(self, req: Request) -> tuple[dict, dict]:
        if not req.questions:
            raise LLMError("this request has no Jev questions; the prompt builder must supply them")
        body = {"model": self.cfg.model, "state": {"rules": req.system, "discussion": req.user},
                "questions": req.questions}
        resp = self.post_json(self.cfg.base_url, body)
        answers = validate_jev_answers(resp.get("answers"), req.questions)
        u = resp.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"),
                 "cost": u.get("cost"), "resolved_model": resp.get("model")}
        return (req.decode(answers) if req.decode else answers), usage


def validate_jev_answers(answers, questions: dict) -> dict:
    """Every question answered, with the right type and finite probabilities; LLMError otherwise."""
    if not isinstance(answers, dict):
        raise LLMError("Jev response has no answers object")
    ok = lambda x: isinstance(x, (int, float)) and x == x and abs(x) != float("inf")  # noqa: E731
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise LLMError(f"Jev answer for {qid!r} missing or of the wrong type: {str(a)[:120]}")
        if q["type"] == "score":
            n = len(q["criteria"])
            if not ok(a.get("score")) or not 0 <= a["score"] <= n - 1:
                raise LLMError(f"Jev score for {qid!r} out of range: {a.get('score')!r}")
        elif q["type"] == "noul":
            if not ok(a.get("noul")) or not 0 <= a["noul"] <= 1:
                raise LLMError(f"Jev noul for {qid!r} is not a probability: {a.get('noul')!r}")
        elif q["type"] == "choice":
            if a.get("choice") not in q["criteria"]:
                raise LLMError(f"Jev choice for {qid!r} is not an option: {a.get('choice')!r}")
        for v in (a.get("probabilities") or {}).values():
            if not ok(v) or not 0 <= v <= 1:
                raise LLMError(f"Jev probabilities for {qid!r} are malformed")
    return answers


def fake_jev_answers(questions: dict, seed: bytes) -> dict:
    """Schema-valid Jev answers derived from ``seed`` (for the fake backend)."""
    out = {}
    for qid, q in questions.items():
        rng = random.Random(seed + qid.encode())
        if q["type"] == "score":
            n = len(q["criteria"])
            out[qid] = {"type": "score", "score": round(rng.random() * (n - 1), 3),
                        "confidence": round(rng.random(), 3)}
        elif q["type"] == "noul":
            out[qid] = {"type": "noul", "noul": round(rng.random(), 3)}
        else:
            opts = list(q["criteria"])
            out[qid] = {"type": "choice", "choice": rng.choice(opts), "confidence": round(rng.random(), 3),
                        "probabilities": {o: round(1 / len(opts), 3) for o in opts}}
    return out


class FakeClient:
    """Deterministic, free. Numbers come from a hash of the request, so tests are stable and
    a dry run exercises every code path. ``fn`` overrides the result for tests."""

    def __init__(self, cfg: Config | None = None, fn=None):
        self.cfg = cfg or Config(model="fake")
        self.fn = fn
        self.calls = 0

    def complete(self, req: Request) -> tuple[dict, dict]:
        self.calls += 1
        if self.fn:
            return self.fn(req), {"prompt_tokens": req.est_in_tokens, "completion_tokens": 100}
        if req.questions and req.decode:   # the Jev path, decoded like the real thing
            answers = fake_jev_answers(req.questions, hashlib.sha1(req.user.encode()).digest())
            return req.decode(answers), {"prompt_tokens": req.est_in_tokens, "completion_tokens": 0}
        return (_fill(req.schema, hashlib.sha1(req.user.encode()).digest()),
                {"prompt_tokens": req.est_in_tokens, "completion_tokens": 100})


def _fill(schema: dict, seed: bytes, depth: int = 0) -> object:
    """A schema-valid value whose numbers are derived from ``seed``."""
    rng = random.Random(seed + bytes([depth]))
    t = schema.get("type")
    if "enum" in schema:
        return rng.choice(schema["enum"])
    if t == "object":
        return {k: _fill(v, hashlib.sha1(seed + k.encode()).digest(), depth + 1)
                for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return []
    if t == "boolean":
        return rng.random() < 0.7
    if t == "integer":
        lo, hi = schema.get("minimum", 0), schema.get("maximum", 100)
        return rng.randint(int(lo), int(hi))
    if t == "number":
        lo, hi = schema.get("minimum", 0.0), schema.get("maximum", 1.0)
        return round(lo + (hi - lo) * rng.random(), 3)
    if t == "string":
        return "fake"
    return None


def make_client(cfg: Config):
    if cfg.model == "fake":
        return FakeClient(cfg)
    return JevClient(cfg) if cfg.backend == "jev" else Client(cfg)


# ------------------------------------------------------------------ running a batch

def run(requests: list[Request], cfg: Config, cache: Cache, spend: Spend, client=None, *,
        dry_run: bool = False, log=print, out_fraction: float = 0.6) -> dict[str, dict]:
    """Complete every request not already cached, under the spend cap. Returns key -> result
    for all requests (cached ones included). Refuses before the first call if the estimate
    would cross the cap; stops mid-run if real spend does."""
    client = client or make_client(cfg)
    results: dict[str, dict] = {}
    todo = []
    for r in requests:
        rec = cache.get(r)
        if rec is not None:
            results[r.key] = rec["result"]
        else:
            todo.append(r)
    est = sum(cfg.cost(r.est_in_tokens, int(r.max_out * out_fraction)) for r in todo)
    log(f"[llm] {len(requests):,} requests, {len(results):,} cached, {len(todo):,} to run; "
        f"estimate ${est:.3f} (upper bound), spent so far ${spend.total:.3f}, cap ${cfg.cap_usd:.2f}, "
        f"model {cfg.model}")
    if dry_run or not todo:
        return results
    if spend.total + est > cfg.cap_usd:
        raise SpendCapError(f"refused: ${spend.total:.3f} spent + ${est:.3f} estimated > cap "
                            f"${cfg.cap_usd:.2f}. Raise LLM_SPEND_CAP_USD or run fewer threads.")
    stop = threading.Event()
    failures: list[tuple[str, str]] = []
    t0 = time.time()
    done = 0

    def one(r: Request):
        """Runs in a worker. Spend is booked here, synchronously with the call, so at most
        ``workers`` calls can be in flight when the cap is crossed."""
        if stop.is_set():
            return r, None, 0.0
        result, usage = client.complete(r)
        if usage.get("cost") is not None:      # the provider's own figure (Jev reports USD)
            cost = float(usage["cost"])
        else:
            cost = cfg.cost(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        total = spend.add(cfg.model, usage, cost, r.key)
        cache.put(r, result, usage, cost, cfg.model)
        if total >= cfg.cap_usd:
            stop.set()
        return r, result, total

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        futs = [pool.submit(one, r) for r in todo]
        for f in as_completed(futs):
            try:
                r, result, total = f.result()
            except LLMError as e:
                failures.append(("?", str(e)))
                continue
            if result is None:
                continue
            results[r.key] = result
            done += 1
            if done % 50 == 0 or done == len(todo):
                rate = done / max(time.time() - t0, 1e-9)
                log(f"[llm] {done:,}/{len(todo):,} done, ${total:.3f} spent, {rate:.1f} req/s")
    if stop.is_set():
        log(f"[llm] spend cap ${cfg.cap_usd:.2f} reached after {done:,} calls: stopped; rerun with a "
            "higher LLM_SPEND_CAP_USD to resume (finished results are cached)")
    if failures:
        log(f"[llm] {len(failures)} requests failed; first: {failures[0][1][:300]}")
    return results


def cache_dir(root: Path, model: str, prompt_version: str, variant: str) -> Path:
    slug = model.replace("/", "_")
    return Path(root) / "cache" / slug / prompt_version / variant


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
