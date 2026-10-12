"""Transport contract: every kernel refusal becomes a structured, code-bearing error.

The module advertises "offline first-class" (docs §21): when the kernel is
unreachable, rejects a claim, fences a completion or answers with garbage, the caller
must get `TransportError` carrying the kernel's own error code — the CLI branches on
`code` to decide between retry, re-claim and hard failure. These tests pin the whole
surface, including the real urllib client against a live socket.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from saf.core.compiler import compile_intent
from saf.transport.effective_scale import EffectiveScaleTransport, TransportError


def _client_returning(status, body):
    calls = []

    def client(method, url, body_req, headers):
        calls.append({"method": method, "url": url, "body": body_req, "headers": headers})
        return status, body

    return calls, client


def _error_body(code, message="no"):
    return {"ok": False, "error": {"code": code, "message": message}}


def _transport(client, **kw):
    return EffectiveScaleTransport("http://127.0.0.1:8080", client=client, **kw)


# ------------------------------------------------------------- error mapping (injected)


@pytest.mark.parametrize(
    "call, code, status",
    [
        (lambda t: t.status("wf-1"), "not_found", 404),
        (lambda t: t.list_attempts(), "internal", 500),
        (lambda t: t.claim_attempt("a-1", worker_id="w"), "conflict", 409),
        (lambda t: t.heartbeat_attempt("a-1", worker_id="w", nonce="n"), "not_found", 404),
        (lambda t: t.complete_attempt("a-1", ok=True, worker_id="w", nonce="n"),
         "conflict", 409),
        (lambda t: t.health(), "unavailable", 503),
        (lambda t: t.submit(compile_intent("run tests")), "conflict", 409),
    ],
)
def test_every_kernel_refusal_is_a_structured_error(call, code, status):
    _, client = _client_returning(status, _error_body(code))
    with pytest.raises(TransportError) as ei:
        asyncio.run(call(_transport(client)))
    assert ei.value.status == status
    assert ei.value.code == code, "the CLI branches on the kernel's own error code"
    assert str(ei.value), "an error must say what failed"


def test_error_without_a_kernel_body_stays_a_transport_error():
    _, client = _client_returning(502, {})
    with pytest.raises(TransportError) as ei:
        asyncio.run(_transport(client).health())
    assert ei.value.status == 502
    assert ei.value.code == "transport_error"
    assert ei.value.body == {}


# ------------------------------------------------------------- request shaping


def test_attempt_query_is_shaped_for_the_kernel():
    calls, client = _client_returning(200, {"attempts": [], "ok": True})
    t = _transport(client)
    asyncio.run(t.list_attempts(claimable=True, state="running", workflow_id="wf-9", limit=3))
    url = calls[-1]["url"]
    assert "/v1/attempts?" in url
    assert "claimable=true" in url and "state=running" in url
    assert "workflow_id=wf-9" in url and "limit=3" in url


def test_claim_payload_includes_ttl_only_when_set():
    calls, client = _client_returning(200, {"ok": True})
    t = _transport(client)
    asyncio.run(t.claim_attempt("a-1", worker_id="w"))
    assert "ttl_seconds" not in calls[-1]["body"]
    asyncio.run(t.claim_attempt("a-1", worker_id="w", ttl_seconds=12.5))
    assert calls[-1]["body"]["ttl_seconds"] == 12.5


def test_completion_payload_carries_error_and_result_only_when_given():
    calls, client = _client_returning(200, {"ok": True})
    t = _transport(client)
    asyncio.run(t.complete_attempt("a-1", ok=False, worker_id="w", nonce="n"))
    sent = calls[-1]["body"]
    assert sent["ok"] is False and sent["nonce"] == "n"
    assert "error" not in sent and "result" not in sent

    asyncio.run(t.complete_attempt("a-1", ok=False, worker_id="w", nonce="n",
                                   error="boom", result={"exit_code": 1}))
    sent = calls[-1]["body"]
    assert sent["error"] == "boom" and sent["result"] == {"exit_code": 1}


def test_heartbeat_and_claim_carry_the_fencing_nonce():
    calls, client = _client_returning(200, {"ok": True})
    t = _transport(client)
    asyncio.run(t.heartbeat_attempt("a-1", worker_id="w", nonce="n-1", ttl_seconds=5))
    sent = calls[-1]["body"]
    assert sent["nonce"] == "n-1" and sent["ttl_seconds"] == 5.0
    assert calls[-1]["headers"].get("Authorization") is None, "no token configured, no header"


def test_bearer_and_idempotency_headers():
    calls, client = _client_returning(
        200, {"workflow": {"id": "wf-1", "status": "running"}, "replayed": True})
    t = _transport(client, token="tok-1")
    out = asyncio.run(t.submit(compile_intent("run tests"), idem_key="saf-key"))
    assert calls[-1]["headers"]["Authorization"] == "Bearer tok-1"
    assert calls[-1]["headers"]["Idempotency-Key"] == "saf-key"
    assert out["replayed"] is True and out["idempotency_key"] == "saf-key"


# ------------------------------------------------------------- real urllib client


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802 — http.server API
        self._respond()

    def do_POST(self):  # noqa: N802 — http.server API
        self._respond()

    def _respond(self):
        payload = self.__class__.routes.get(self.path.split("?")[0], (200, b"{}"))
        status, raw = payload
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture()
def kernel_stub():
    """A real socket speaking the kernel's error dialect (and one malformed reply)."""
    _Handler.routes = {
        "/v1/workflows": (409, json.dumps(_error_body("conflict", "key reused")).encode()),
        "/v1/health/ready": (200, b"not json at all"),
    }
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_default_client_maps_a_live_http_error_body(kernel_stub):
    t = EffectiveScaleTransport(kernel_stub, client=None)
    with pytest.raises(TransportError) as ei:
        asyncio.run(t.submit(compile_intent("run tests")))
    assert ei.value.status == 409
    assert ei.value.code == "conflict"
    assert ei.value.body["error"]["message"] == "key reused"


def test_default_client_rejects_a_malformed_kernel_response(kernel_stub):
    t = EffectiveScaleTransport(kernel_stub, client=None)
    with pytest.raises(TransportError) as ei:
        asyncio.run(t.health())
    assert "malformed response" in str(ei.value)


def test_default_client_reports_an_unreachable_kernel():
    t = EffectiveScaleTransport("http://127.0.0.1:1", client=None, timeout_s=1.0)
    with pytest.raises(TransportError) as ei:
        asyncio.run(t.health())
    assert "unreachable" in str(ei.value)
    assert ei.value.status is None, "a socket failure has no HTTP status"
