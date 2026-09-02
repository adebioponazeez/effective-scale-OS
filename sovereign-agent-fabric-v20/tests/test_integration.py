"""SAF → effective-scale-OS integration proofs.

Unit tests use an injected HTTP client (no network). The live test boots a
real embedded kernel (Kernel + ApiServer) and drives it over HTTP through the
transport's default urllib client — the same path the CLI uses.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from saf.core.compiler import compile_intent
from saf.transport.effective_scale import (
    EffectiveScaleTransport,
    TransportError,
    idempotency_key,
    slug,
)


def _stub_client(bucket):
    """Injectable client: records requests, returns canned kernel responses."""

    def client(method, url, body, headers):
        bucket.append({"method": method, "url": url, "body": body, "headers": headers})
        if "/health/ready" in url:
            return 200, {"ok": True, "ready": True}
        if url.endswith("/v1/workflows") and method == "POST":
            return 200, {"workflow": {"id": "wf-1", "status": "running"}, "replayed": False}
        if "wf-1" in url:
            return 200, {"workflow": {"id": "wf-1", "status": "succeeded"}}
        return 404, {"ok": False, "error": {"code": "not_found", "message": "no"}}

    return client


# ----------------------------------------------------------------- unit level


def test_slug_normalization():
    assert slug("cap://software/code/refactor") == "cap-software-code-refactor"
    assert slug("  Mixed Case  ") == "mixed-case"


def test_idempotency_key_is_deterministic_and_stable():
    a = compile_intent("refactor repository and run tests")
    b = compile_intent("refactor repository and run tests")
    c = compile_intent("run tests only")
    assert idempotency_key(a) == idempotency_key(b)
    assert idempotency_key(a) != idempotency_key(c)
    assert idempotency_key(a).startswith("saf-")


def test_submit_builds_valid_plan_and_bearer_auth():
    seen = []
    t = EffectiveScaleTransport(
        "http://127.0.0.1:8080", token="tok-1", client=_stub_client(seen), node_timeout_s=42.0
    )
    task = compile_intent("refactor repository and run tests")
    out = asyncio.run(t.submit(task))

    assert out["workflow"]["id"] == "wf-1"
    assert out["idempotency_key"] == idempotency_key(task)
    req = seen[-1]
    assert req["headers"]["Authorization"] == "Bearer tok-1"
    assert req["headers"]["Idempotency-Key"] == idempotency_key(task)
    wf = req["body"]
    assert wf["name"] == "saf/refactor-repository-and-run-tests"
    topics = [n["exec"]["topic"] for n in wf["nodes"]]
    assert "saf.task.cap-software-code-refactor" in topics
    assert "saf.task.cap-software-testing-execute" in topics
    assert all(n["timeout"] == 42.0 for n in wf["nodes"])
    assert wf["dead_letter"] is True


def test_status_query():
    seen = []
    t = EffectiveScaleTransport("http://127.0.0.1:8080", client=_stub_client(seen))
    out = asyncio.run(t.status("wf-1"))
    assert out["status"] == "succeeded"


def test_error_mapping():
    def failing(method, url, body, headers):
        raise TransportError("connection refused")

    t = EffectiveScaleTransport("http://127.0.0.1:1", client=failing)
    with pytest.raises(TransportError):
        asyncio.run(t.submit(compile_intent("hello")))


def test_http_error_carries_kernel_code():
    def conflict(method, url, body, headers):
        return 409, {"ok": False, "error": {"code": "conflict", "message": "key reused"}}

    t = EffectiveScaleTransport("http://127.0.0.1:8080", client=conflict)
    with pytest.raises(TransportError) as ei:
        asyncio.run(t.submit(compile_intent("hello")))
    assert ei.value.status == 409
    assert ei.value.code == "conflict"


# ------------------------------------------------------------------ live level


def _find_effective_scale_src():
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "src"
        if (candidate / "effective_scale").is_dir():
            return candidate
    return None


@pytest.mark.skipif(_find_effective_scale_src() is None,
                    reason="effective-scale-OS source not present (repo sibling)")
def test_live_submit_against_real_kernel(tmp_path):
    src = _find_effective_scale_src()
    sys.path.insert(0, str(src))

    from effective_scale.adapters.memory_store import MemoryStore
    from effective_scale.api.server import ApiServer
    from effective_scale.core.kernel import Config, Kernel
    from effective_scale.ports.logger import MemLogger

    cfg = Config(
        store_path=":memory:", listen="127.0.0.1:0",
        auth_secret="test-secret-0123456789abcdef", admin_token="bootstrap-test-token",
        scheduler_interval=0.05, scale_interval=0.05, workflow_interval=0.02,
        event_interval=0.02, heartbeat_ttl=1.0, watchdog_stall=30.0,
    )
    kernel = Kernel(MemoryStore(), config=cfg, logger=MemLogger())
    kernel.start()
    api = ApiServer(kernel)
    api.start()
    port = api._httpd.server_address[1]
    try:
        from http.client import HTTPConnection

        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/tokens",
                     body=json.dumps({"namespace": "demo", "scopes": ["read", "write", "admin"]}),
                     headers={"Content-Type": "application/json",
                              "X-Admin-Token": "bootstrap-test-token"})
        resp = conn.getresponse()
        token = json.loads(resp.read())["token"]
        conn.close()

        t = EffectiveScaleTransport(
            f"http://127.0.0.1:{port}", token=token, node_timeout_s=30.0, retry_max=2
        )
        task = compile_intent("refactor repository and run tests")
        submitted = asyncio.run(t.submit(task))
        wid = submitted["workflow"]["id"]

        # Replay: same idempotency key + payload returns the same workflow.
        replayed = asyncio.run(t.submit(task))
        assert replayed["workflow"]["id"] == wid
        assert replayed["replayed"] is True

        # The kernel must observe the plan: its sweep loop dispatches nodes
        # asynchronously; event nodes fire-and-complete on durable publish.
        import time

        def workflow_observed() -> bool:
            snap = kernel.store.snapshot()
            wf = snap.workflow(wid)
            return wf.status.value in {"running", "succeeded"}

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not workflow_observed():
            time.sleep(0.03)
        assert workflow_observed()

        snap = kernel.store.snapshot()
        topics = {n.event_topic for n in snap.workflow(wid).nodes.values()}
        assert "saf.task.cap-software-code-refactor" in topics
    finally:
        api.stop()
        kernel.stop()
