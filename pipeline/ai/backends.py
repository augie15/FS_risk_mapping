"""Two vision backends behind one interface: local Ollama and the Claude API.

Every call is logged (tokens, cost, latency) to data/ai_calls.jsonl.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from ..paths import DATA

ROOT = Path(__file__).resolve().parents[2]
CALL_LOG = DATA / "ai_calls.jsonl"



def _ollama_url() -> str:
    """Ollama base URL from OLLAMA_HOST (Ollama's own variable: "host", "host:port" or a URL).

    OLLAMA_URL is still read for older .env files. Default: this machine.
    """
    raw = (os.getenv("OLLAMA_HOST") or os.getenv("OLLAMA_URL") or "localhost:11434").strip().rstrip("/")
    if "://" in raw:
        return raw                                 # a full URL is used as given
    hostport = raw.split("/", 1)[0]
    if ":" not in hostport.rsplit("]", 1)[-1]:     # bare host: Ollama's default port (IPv6 brackets ok)
        raw = raw.replace(hostport, hostport + ":11434", 1)
    return "http://" + raw


OLLAMA_URL = _ollama_url()
OLLAMA_MODEL = os.getenv("FS_OLLAMA_MODEL", "qwen3-vl:8b-instruct")
CLAUDE_MODEL = os.getenv("FS_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("FS_CLAUDE_EFFORT", "medium")
CLAUDE_MAX_TOKENS = 8000
# Hard cap on Claude spend per calendar day (server local time), summed from data/ai_calls.jsonl.
DAILY_CAP_ENV = "FS_CLAUDE_DAILY_CAP_USD"
DAILY_CAP_DEFAULT = 5.0
# USD per million tokens (input, output), first-party API rates as of 2026-11.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.0, 40.0),
}


class SpendCapExceeded(RuntimeError):
    pass


class DailyCapReached(SpendCapExceeded):
    """Today's Claude spend plus the next call's worst case would pass the daily cap."""


def daily_cap_usd() -> float:
    return float(os.getenv(DAILY_CAP_ENV, str(DAILY_CAP_DEFAULT)))


def spent_today_usd(backend: str = "claude") -> float:
    """Logged spend for today (local date) for one backend."""
    if not CALL_LOG.exists():
        return 0.0
    today = time.strftime("%Y-%m-%d")
    total = 0.0
    with CALL_LOG.open() as fh:
        for line in fh:
            if not line.startswith('{"ts": "' + today):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("backend") == backend:
                total += float(row.get("cost_usd") or 0.0)
    return total


# Worst-case cost of Claude calls in flight in this process, so two runs at once cannot both
# squeeze under the daily cap. The service runs one worker, so a process lock is enough.
_inflight_lock = threading.Lock()
_inflight_usd = 0.0
_unlogged_usd = 0.0   # spent by finished calls that log_call has not written yet


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
        self.offline = False

    def available(self):
        try:
            tags = requests.get(f"{OLLAMA_URL}/api/tags", timeout=(2, 4)).json()["models"]
        except Exception:
            self.offline = True
            return False, (f"Local AI offline: Ollama at {OLLAMA_URL} is not answering "
                           "(the Mac running it may be asleep or off the VPN)")
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
        today, cap = spent_today_usd(), daily_cap_usd()
        if today >= cap:
            return False, f"Daily Claude cap reached: ${today:.2f} of ${cap:.2f} spent today"
        return True, (f"Claude {self.model}, cap ${self.cap_usd:.2f} per run, "
                      f"${today:.2f} of ${cap:.2f} spent today")

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
        global _inflight_usd, _unlogged_usd
        with _inflight_lock:
            today, cap = spent_today_usd(), daily_cap_usd()
            if today + _inflight_usd + _unlogged_usd + worst > cap:
                raise DailyCapReached(f"daily Claude cap: next call could cost up to ${worst:.3f}; "
                                      f"${today:.2f} of ${cap:.2f} already spent today")
            _inflight_usd += worst
        try:
            res = self._send(messages, system, schema, res)
        finally:
            with _inflight_lock:
                _inflight_usd -= worst
                _unlogged_usd += res.cost_usd
        return res

    def _send(self, messages, system, schema, res):
        import anthropic
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
    global _unlogged_usd
    with _inflight_lock:
        _log(res, context)
        if res.backend == "claude":
            _unlogged_usd = max(0.0, _unlogged_usd - res.cost_usd)
