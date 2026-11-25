"""Two vision backends behind one interface: local Ollama and the Claude API.

Every call is logged (tokens, cost, latency) to data/ai_calls.jsonl.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
CALL_LOG = ROOT / "data" / "ai_calls.jsonl"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("FS_OLLAMA_MODEL", "qwen3-vl:8b-instruct")
CLAUDE_MODEL = os.getenv("FS_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("FS_CLAUDE_EFFORT", "medium")
CLAUDE_MAX_TOKENS = 8000
# USD per million tokens (input, output), first-party API rates as of 2026-11.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}


class SpendCapExceeded(RuntimeError):
    pass


class BackendUnusable(RuntimeError):
    """Bad credentials or similar: stop the whole run, write nothing."""


@dataclass
class CallResult:
    backend: str
    model: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None
    extra: dict = field(default_factory=dict)


def _log(rec: CallResult, context: dict) -> None:
    CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **context, **asdict(rec)}
    row.pop("text", None)
    with CALL_LOG.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


class Backend:
    name = "base"
    model = ""

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def call(self, system: str, text: str, image_png: bytes | None, schema: dict) -> CallResult:
        raise NotImplementedError


class OllamaBackend(Backend):
    name = "local"

    def __init__(self, model: str = OLLAMA_MODEL):
        self.model = model

    def available(self):
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()["models"]
        except Exception as e:
            return False, f"Ollama not reachable at {OLLAMA_URL}: {e}"
        names = [m["name"] for m in tags]
        if self.model not in names:
            return False, f"Ollama model {self.model} not installed (have: {', '.join(names)})"
        return True, f"Ollama {self.model}"

    def call(self, system, text, image_png, schema):
        msg: dict = {"role": "user", "content": text}
        if image_png:
            msg["images"] = [base64.b64encode(image_png).decode()]
        body = {"model": self.model, "stream": False, "format": schema, "keep_alive": "15m",
                "options": {"temperature": 0, "num_ctx": 16384},
                "messages": [{"role": "system", "content": system}, msg]}
        t0 = time.time()
        res = CallResult(self.name, self.model)
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=900)
            r.raise_for_status()
            d = r.json()
            res.text = d["message"]["content"]
            res.input_tokens = d.get("prompt_eval_count", 0)
            res.output_tokens = d.get("eval_count", 0)
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
        res.latency_s = round(time.time() - t0, 2)
        return res


_key_checks: dict[tuple[str, str], str | None] = {}


def _key_problem(key: str, model: str) -> str | None:
    """Free check (Models API) that the key works, cached per key+model for this process."""
    import hashlib
    cache_key = (hashlib.sha256(key.encode()).hexdigest(), model)
    if cache_key not in _key_checks:
        import anthropic
        try:
            anthropic.Anthropic(api_key=key, max_retries=0, timeout=15).models.retrieve(model)
            _key_checks[cache_key] = None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            _key_checks[cache_key] = f"ANTHROPIC_API_KEY in .env was rejected by the API ({e.status_code})"
        except anthropic.NotFoundError:
            _key_checks[cache_key] = f"Model {model} not available to this key"
        except anthropic.APIError as e:
            return f"Could not reach the Claude API: {e}"  # don't cache network blips
    return _key_checks[cache_key]


class ClaudeBackend(Backend):
    """Claude API via the official SDK. Key comes from ANTHROPIC_API_KEY in .env only."""
    name = "claude"

    def __init__(self, model: str = CLAUDE_MODEL, cap_usd: float | None = None):
        self.model = model
        self.cap_usd = cap_usd if cap_usd is not None else float(os.getenv("FS_AI_RUN_CAP_USD", "0.50"))
        self.spent_usd = 0.0
        self._client = None

    def available(self):
        if not os.getenv("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY is not set in .env"
        if self.model not in PRICES:
            return False, f"No price on file for {self.model}; add it to PRICES before running"
        bad = _key_problem(os.environ["ANTHROPIC_API_KEY"], self.model)
        if bad:
            return False, bad
        return True, f"Claude {self.model}, cap ${self.cap_usd:.2f} per run"

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def _cost(self, model: str, inp: int, out: int, cache_w: int = 0, cache_r: int = 0) -> float:
        pin, pout = PRICES.get(model, PRICES[self.model])
        return (inp * pin + out * pout + cache_w * pin * 1.25 + cache_r * pin * 0.1) / 1e6

    def call(self, system, text, image_png, schema):
        import anthropic
        content = []
        if image_png:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": base64.b64encode(image_png).decode()}})
        content.append({"type": "text", "text": text})
        messages = [{"role": "user", "content": content}]
        res = CallResult(self.name, self.model)

        # Spend cap: refuse the call if its worst case (all max_tokens used) could pass the cap.
        try:
            n_in = self.client.messages.count_tokens(model=self.model, system=system, messages=messages).input_tokens
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise BackendUnusable(f"Claude API rejected the key in .env ({e.status_code}); fix ANTHROPIC_API_KEY") from e
        except anthropic.APIError as e:
            res.error = f"count_tokens failed: {e}"
            return res
        worst = self._cost(self.model, n_in, CLAUDE_MAX_TOKENS)
        if self.spent_usd + worst > self.cap_usd:
            raise SpendCapExceeded(f"next call could cost up to ${worst:.3f}; spent ${self.spent_usd:.3f} "
                                   f"of ${self.cap_usd:.2f} cap")
        t0 = time.time()
        try:
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=CLAUDE_MAX_TOKENS, system=system, messages=messages,
                output_config={"effort": CLAUDE_EFFORT, "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise BackendUnusable(f"Claude API rejected the key in .env ({e.status_code})") from e
        except anthropic.RateLimitError as e:
            res.error = f"rate limited: {e.message}"
        except anthropic.APIStatusError as e:
            res.error = f"API error {e.status_code}: {e.message}"
        except anthropic.APIConnectionError as e:
            res.error = f"connection error: {e}"
        else:
            u = resp.usage
            res.input_tokens = u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
            res.output_tokens = u.output_tokens
            res.cost_usd = round(self._cost(resp.model, u.input_tokens, u.output_tokens,
                                            u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0), 5)
            self.spent_usd += res.cost_usd
            res.extra = {"served_by": resp.model, "stop_reason": resp.stop_reason, "request_id": resp._request_id}
            if resp.stop_reason == "refusal":
                res.error = "model refused"
            elif resp.stop_reason == "max_tokens":
                res.error = "hit max_tokens"
            else:
                res.text = next((b.text for b in resp.content if b.type == "text"), "")
        res.latency_s = round(time.time() - t0, 2)
        return res


def get(name: str) -> Backend:
    if name == "local":
        return OllamaBackend()
    if name == "claude":
        return ClaudeBackend()
    raise ValueError(f"unknown backend {name}")


def log_call(res: CallResult, **context) -> None:
    _log(res, context)
