"""Offline tests for the AI layer: validator, LiDAR cross-checks, and the Claude backend
against a local stub server (no network, no spend)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pipeline.ai import backends, checklist, prompt, validate


def test_checklist_ids_unique_and_schema_enums():
    ids = [i["id"] for z in checklist.load()["zones"] for i in z["items"]]
    assert len(ids) == len(set(ids))
    for z in checklist.load()["zones"]:
        enum = prompt.schema(z["zone"])["properties"]["findings"]["items"]["properties"]["item_id"]["enum"]
        assert enum == [i["id"] for i in z["items"]]


def test_parse_fills_missing_and_drops_bad():
    text = json.dumps({"zone": "intermediate", "findings": [
        {"item_id": "int_conifers", "status": "ok", "confidence": 1.4, "reason": "No conifers.", "location": {"x": 5000, "y": 1}},
        {"item_id": "int_conifers", "status": "concern", "confidence": 0.5, "reason": "dup", "location": None},
        {"item_id": "made_up", "status": "ok", "confidence": 0.5, "reason": "x", "location": None},
        {"item_id": "int_woodpile", "status": "maybe", "confidence": 0.5, "reason": "x", "location": None},
    ]})
    f, problems = validate.parse_and_check(text, "intermediate", {"width": 768, "height": 700})
    by = {x["item_id"]: x for x in f}
    assert len(f) == 5
    assert by["int_conifers"]["confidence"] == 1.0 and by["int_conifers"]["location"] is None
    assert by["int_woodpile"]["status"] == "cant_tell"
    assert by["int_grass_mulch"]["schema_ok"] is False
    assert any("duplicate" in p for p in problems) and any("made_up" in p for p in problems)


def test_garbage_answer_becomes_all_cant_tell():
    f, problems = validate.parse_and_check("not json", "outer", None)
    assert [x["status"] for x in f] == ["cant_tell"] * 3 and problems


def _f(item, status, reason="x"):
    return {"item_id": item, "status": status, "reason": reason, "confidence": 0.9, "location": None}


def test_lidar_catches_no_trees_claim():
    facts = {"lidar_available": True, "tree_min_height_m": 3, "trees_with_trunk_in_zone": 3}
    v = validate.lidar_check(_f("int_conifers", "ok", "No conifers or trees within 10 m."), facts)
    assert v["result"] == "disagree"
    v = validate.lidar_check(_f("int_conifers", "ok", "Only birch near the house."), facts)
    assert v["result"] == "partial"


def test_lidar_crowns_touching_and_slope():
    v = validate.lidar_check(_f("imm_overhanging_trees", "ok"), {"lidar_available": True, "crowns_touching_home": 2})
    assert v["result"] == "disagree" and v["lidar_status"] == "concern"
    flat = {"lidar_available": True, "mean_slope_deg_30_100m": 9.3, "ground_drop_below_home_m": 0.4}
    assert validate.lidar_check(_f("out_slope", "concern"), flat)["result"] == "disagree"
    steep = {"lidar_available": True, "mean_slope_deg_30_100m": 18.0, "ground_drop_below_home_m": 25.0}
    assert validate.lidar_check(_f("out_slope", "concern"), steep)["result"] == "agree"


def test_unknown_tree_refs():
    assert validate.unknown_tree_refs("Near T5 and T99", {"T5", "T6"}) == ["T99"]


# ---------------------------------------------------------------- Claude backend vs stub server

class _Stub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.seen.append((self.path, body, dict(self.headers)))
        if self.path.startswith("/v1/messages/count_tokens"):
            out = {"input_tokens": 2000}
        else:
            answer = {"zone": "outer", "findings": [
                {"item_id": i, "status": "cant_tell", "confidence": 0.5, "reason": "stub", "location": None}
                for i in ("out_fuel_type", "out_continuous_forest", "out_slope")]}
            out = {"id": "msg_stub", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                   "content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": "end_turn",
                   "stop_sequence": None,
                   "usage": {"input_tokens": 2000, "output_tokens": 500,
                             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(autouse=True)
def _isolated_spend(tmp_path, monkeypatch):
    """Never read or write the real data/ai_calls.jsonl; start each test with nothing in flight."""
    monkeypatch.setattr(backends, "CALL_LOG", tmp_path / "ai_calls.jsonl")
    monkeypatch.setattr(backends, "_unlogged_usd", 0.0)
    monkeypatch.setattr(backends, "_inflight_usd", 0.0)


@pytest.fixture
def stub_client():
    import anthropic
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Stub.seen.clear()
    yield anthropic.Anthropic(api_key="test-key", base_url=f"http://127.0.0.1:{srv.server_port}", max_retries=0)
    srv.shutdown()


def test_claude_request_shape_cost_and_cap(stub_client):
    be = backends.ClaudeBackend(model="claude-opus-5-5", cap_usd=0.20)
    be._client = stub_client
    res = be.call(prompt.SYSTEM, "facts", b"\x89PNG fake", prompt.schema("outer"))
    assert res.error is None
    assert res.cost_usd == pytest.approx((2000 * 4 + 500 * 20) / 1e6)
    f, problems = validate.parse_and_check(res.text, "outer", None)
    assert not problems and len(f) == 3
    path, body, headers = _Stub.seen[-1]
    assert path.startswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == backends.CLAUDE_EFFORT
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    # Worst case per call is $0.168 (2000 in + 8000 out); actual is $0.018. Cap $0.20: calls 1-2 pass, 3 is refused.
    be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))
    with pytest.raises(backends.SpendCapExceeded):
        be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))


def test_concurrent_lidar_download_is_single_and_complete(tmp_path, monkeypatch):
    """Two threads asking for the same tile: one download, one intact file, no leftovers."""
    import time as _time
    from pipeline import sources

    payload = b"x" * (3 << 20)
    calls = []

    class _Resp:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            for i in range(0, len(payload), 1 << 20):
                _time.sleep(0.05)  # widen the race window
                yield payload[i:i + (1 << 20)]

    def fake_get(url, **kw):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(sources, "RAW", tmp_path)
    monkeypatch.setattr(sources.requests, "get", fake_get)
    tile = {"filename": "t.laz", "s3Url": "https://example.invalid/t.laz"}
    out = []
    ts = [threading.Thread(target=lambda: out.append(sources.download_lidar(tile))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1
    assert (tmp_path / "lidar" / "t.laz").read_bytes() == payload
    assert not list((tmp_path / "lidar").glob("*.part"))


def test_truncated_lidar_download_is_not_cached(tmp_path, monkeypatch):
    from pipeline import sources

    class _Short:
        headers = {"Content-Length": "100"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"x" * 40

    monkeypatch.setattr(sources, "RAW", tmp_path)
    monkeypatch.setattr(sources.requests, "get", lambda url, **kw: _Short())
    with pytest.raises(IOError):
        sources.download_lidar({"filename": "s.laz", "s3Url": "https://example.invalid/s.laz"})
    assert not (tmp_path / "lidar" / "s.laz").exists()
    assert not list((tmp_path / "lidar").glob("*.part"))


# ---------------------------------------------------------------- deploy settings: daily cap, Ollama host

def test_daily_cap_refuses_before_sending(stub_client, monkeypatch):
    log = backends.CALL_LOG
    monkeypatch.setenv("FS_CLAUDE_DAILY_CAP_USD", "0.30")
    today = time.strftime("%Y-%m-%d")
    log.write_text(
        json.dumps({"ts": f"{today}T08:00:00", "backend": "claude", "cost_usd": 0.10}) + "\n"
        + json.dumps({"ts": f"{today}T08:01:00", "backend": "local", "cost_usd": 9.0}) + "\n"
        + json.dumps({"ts": "2001-01-01T08:00:00", "backend": "claude", "cost_usd": 9.0}) + "\n")
    assert backends.spent_today_usd() == pytest.approx(0.10)
    be = backends.ClaudeBackend(model="claude-opus-5-5", cap_usd=10.0)
    be._client = stub_client
    # $0.10 spent + $0.168 worst case = $0.268 <= $0.30: allowed.
    res = be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))
    backends.log_call(res, property_id="x", zone="outer")
    assert backends.spent_today_usd() == pytest.approx(0.118)
    n_sent = sum(1 for p, _, _ in _Stub.seen if not p.startswith("/v1/messages/count_tokens"))
    # $0.118 + $0.168 = $0.286 still fits; then $0.136 + $0.168 does not.
    backends.log_call(be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer")), property_id="x", zone="outer")
    with pytest.raises(backends.DailyCapReached):
        be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))
    sent_after = sum(1 for p, _, _ in _Stub.seen if not p.startswith("/v1/messages/count_tokens"))
    assert sent_after == n_sent + 1          # the refused call never reached the messages endpoint


def test_daily_cap_counts_unlogged_calls(stub_client, monkeypatch):
    """A finished call that is not logged yet still counts against the cap."""
    monkeypatch.setenv("FS_CLAUDE_DAILY_CAP_USD", "0.20")
    be = backends.ClaudeBackend(model="claude-opus-5-5", cap_usd=10.0)
    be._client = stub_client
    be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))   # 0     + 0.168 fits; costs 0.018
    be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))   # 0.018 + 0.168 fits
    with pytest.raises(backends.DailyCapReached):                   # 0.036 + 0.168 > 0.20
        be.call(prompt.SYSTEM, "facts", None, prompt.schema("outer"))
    assert backends.spent_today_usd() == 0.0                        # nothing logged


@pytest.mark.parametrize("value,url", [
    (None, "http://localhost:11434"),
    ("10.10.0.5", "http://10.10.0.5:11434"),
    ("10.10.0.5:8080", "http://10.10.0.5:8080"),
    ("http://mac.wg:11434/", "http://mac.wg:11434"),
    ("[fd00::2]", "http://[fd00::2]:11434"),
])
def test_ollama_host_forms(monkeypatch, value, url):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    if value is None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
    else:
        monkeypatch.setenv("OLLAMA_HOST", value)
    assert backends._ollama_url() == url


def test_ollama_unreachable_is_offline(monkeypatch):
    monkeypatch.setattr(backends, "OLLAMA_URL", "http://127.0.0.1:9")   # discard port: refused
    be = backends.OllamaBackend()
    ok, why = be.available()
    assert not ok and be.offline and why.startswith("Local AI offline")
