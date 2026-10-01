"""HTTP control plane: stdlib-only server with auth, idempotency, rate limiting.

Design notes:
  * `ThreadingHTTPServer` — battle-tested parse/serve; handlers are thin.
  * All mutations go through `kernel.write(...)` (single-writer queue).
  * Client errors are JSON with a stable `code`; server never leaks stack traces.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..core.kernel import Kernel
from ..domain.errors import DomainError, Forbidden, RateLimited, Unauthorized
from ..domain.models import to_jsonable
from ..domain.states import NodeState

MAX_BODY = 1 << 20  # 1 MiB


class RateLimiter:
    """Sliding-window counter per key — honest, simple, observable."""

    def __init__(self, limit_per_minute: int):
        self._limit = limit_per_minute
        self._windows: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        now = time.time()
        with self._lock:
            count, window_start = self._windows.get(key, (0, now))
            if now - window_start >= 60.0:
                count, window_start = 0, now
            if count >= self._limit:
                return False, 60.0 - (now - window_start)
            self._windows[key] = (count + 1, window_start)
            return True, 0.0


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "effective-scale/0.4"
    protocol_version = "HTTP/1.1"

    # injected by ApiServer.__init__
    kernel: Kernel
    limiter: RateLimiter
    routes: list[Any]

    # ---------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args) -> None:  # silence default access log
        return None

    def _send(self, status: int, body: dict, headers: dict[str, str] | None = None,
              trace_id: str = "") -> None:
        payload = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Trace-Id", trace_id)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, code: str, message: str, *, trace_id: str = "",
               headers: dict[str, str] | None = None) -> None:
        self._send(status, {"ok": False, "error": {"code": code, "message": message}},
                   headers=headers, trace_id=trace_id)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY:
            raise DomainError("request body too large", details={})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise DomainError("invalid JSON body")
        if not isinstance(data, dict):
            raise DomainError("JSON body must be an object")
        return data

    def _auth(self, *, namespace: str | None = None, scope: str = "read"):
        raw = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return self.kernel.auth.require(raw, namespace=namespace, scope=scope)

    def _route(self, method: str, path: str):
        for pattern, handler in self.routes:
            if method not in pattern:
                continue
            m = handler["regex"].match(path)
            if m:
                return handler, m.groupdict()
        return None, {}

    # ---------------------------------------------------------------- dispatch

    def _dispatch(self, method: str) -> None:
        trace_id = self.headers.get("X-Trace-Id", "") or f"req-{id(self):x}"
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        handler, params = self._route(method, path)
        if handler is None:
            self._error(404, "not_found", f"no route for {method} {path}", trace_id=trace_id)
            return
        ok, retry_after = self.limiter.allow(f"{self.client_address[0]}:{path.split('/')[1]}")
        if not ok:
            self._error(RateLimited.http_status, RateLimited.code, "rate limit exceeded",
                        trace_id=trace_id, headers={"Retry-After": str(int(retry_after) + 1)})
            return
        # bootstrap-admin gate: X-Admin-Token matches config, or a real admin token
        self._bootstrap = False
        admin_header = self.headers.get("X-Admin-Token", "")
        if handler.get("admin"):
            if admin_header and self.kernel.config.admin_token and \
                    admin_header == self.kernel.config.admin_token:
                self._bootstrap = True
            elif not self.kernel.config.admin_token:
                self._bootstrap = True  # dev mode: no bootstrap secret configured
        try:
            result = handler["fn"](self, params, qs, trace_id)
            if result is not None:
                status, body, extra = result
                self._send(status, body, headers=extra, trace_id=trace_id)
        except _Replay as exc:
            # replay is shaped identically to the original response, plus a marker
            self._send(exc.status, {"replayed": True, **exc.body}, trace_id=trace_id)
        except Unauthorized as exc:
            self._error(exc.http_status, exc.code, exc.message, trace_id=trace_id)
        except Forbidden as exc:
            self._error(exc.http_status, exc.code, exc.message, trace_id=trace_id)
        except RateLimited as exc:
            self._error(exc.http_status, exc.code, exc.message, trace_id=trace_id,
                        headers={"Retry-After": "1"})
        except DomainError as exc:
            self._error(exc.http_status, exc.code, exc.message, trace_id=trace_id)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 — last-resort boundary
            self.kernel.logger.log("api.unhandled", error=True, path=path, error_msg=str(exc))
            self._error(500, "internal_error", "internal server error", trace_id=trace_id)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PUT(self) -> None:
        self._dispatch("PUT")


# ---------------------------------------------------------------- route fs


def _match(pattern: str) -> str:
    parts = [p for p in pattern.split("/") if p]
    regex = "^/" + "/".join(
        f"(?P<{p[1:-1]}>[^/]+)" if p.startswith("{") else p for p in parts
    )
    return regex + "$"


class ApiServer:
    def __init__(self, kernel: Kernel):
        self.kernel = kernel
        self.limiter = RateLimiter(kernel.config.rate_limit_per_minute)
        self.routes: list[Any] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        """The bound TCP port (useful when the configured port is 0)."""
        return self._httpd.server_address[1] if self._httpd else None

    def route(self, methods: str, pattern: str, fn: Callable, *, public: bool = False,
              scope: str = "read", admin: bool = False) -> None:
        import re

        compiled = re.compile(_match(pattern))
        self.routes.append((methods.split(","), {"regex": compiled, "fn": fn,
                                                 "public": public, "scope": scope, "admin": admin}))

    def _live(self, h, p, q, t):
        """Liveness: the process is up and its loops are alive. Never touches the store."""
        live = self.kernel.running()
        return 200 if live else 503, {"ok": live, "live": live}, {}

    def _ready(self, h, p, q, t):
        """Readiness: the kernel can actually commit. A dead store or a wedged writer
        must report not-ready, never a stack trace."""
        try:
            ready = self.kernel.ready()
        except Exception:  # noqa: BLE001 — a probe must never raise
            ready = False
        if not ready:
            return 503, {"ok": False, "ready": False, "error": "kernel_not_ready"}, {}
        return 200, {"ok": True, "ready": True}, {}

    def _registered(self) -> None:
        k = self.kernel
        r = self.route

        r("GET", "/v1/health/live", self._live, public=True)
        r("GET", "/v1/health/ready", self._ready, public=True)
        r("GET", "/v1/status", self._status, public=True)
        r("GET", "/v1/metrics", self._metrics, public=True)

        r("GET", "/v1/me", self._me, scope="read")
        r("GET", "/v1/namespaces", lambda h, p, q, t: (200, {"namespaces": sorted(
            k.store.snapshot().namespaces.keys())}, {}), scope="read")
        r("POST", "/v1/namespaces", self._create_namespace, scope="write", admin=True)
        r("POST", "/v1/tokens", self._issue_token, scope="admin", admin=True)
        r("GET", "/v1/audit", self._audit, scope="admin", admin=True)

        r("POST", "/v1/workloads", self._create_workload, scope="write")
        r("GET", "/v1/workloads", self._list_workloads, scope="read")
        r("GET", "/v1/workloads/{wid}", self._get_workload, scope="read")
        r("DELETE", "/v1/workloads/{wid}", self._delete_workload, scope="write")
        r("POST", "/v1/workloads/{wid}/scale", self._scale_workload, scope="write")
        r("POST", "/v1/workloads/{wid}/metrics", self._ingest_metrics, scope="write")

        r("POST", "/v1/nodes", self._create_node, scope="write", admin=True)
        r("GET", "/v1/nodes", self._list_nodes, scope="read")
        r("POST", "/v1/nodes/{nid}/cordon", self._cordon_node, scope="write", admin=True)
        r("POST", "/v1/nodes/{nid}/drain", self._drain_node, scope="write", admin=True)

        r("POST", "/v1/workflows", self._create_workflow, scope="write")
        r("GET", "/v1/workflows", self._list_workflows, scope="read")
        r("GET", "/v1/workflows/{wid}", self._get_workflow, scope="read")
        r("POST", "/v1/workflows/{wid}/cancel", self._cancel_workflow, scope="write")
        r("POST", "/v1/workflows/{wid}/retry-node/{nid}", self._retry_node, scope="write")
        # External worker protocol (ADR-006): discover -> claim -> heartbeat -> complete.
        r("GET", "/v1/attempts", self._list_attempts, scope="read")
        r("GET", "/v1/attempts/{aid}", self._get_attempt, scope="read")
        r("POST", "/v1/attempts/{aid}/claim", self._claim_attempt, scope="write")
        r("POST", "/v1/attempts/{aid}/heartbeat", self._heartbeat_attempt, scope="write")
        r("POST", "/v1/attempts/{aid}/complete", self._complete_attempt, scope="write")
        r("GET", "/v1/leases", self._list_leases, scope="read")

        r("POST", "/v1/events", self._publish_event, scope="write")
        r("GET", "/v1/events/{topic}", self._fetch_events, scope="read")
        r("POST", "/v1/events/{topic}/ack", self._ack_event, scope="write")
        r("GET", "/v1/events/{topic}/dlq", self._list_dlq, scope="read")

    # ---------------------------------------------------------------- handlers

    def _auth_for(self, handler, *, scope: str = "read", namespace: str | None = None):
        return handler._auth(namespace=namespace, scope=scope)

    def _status(self, h, p, q, t):
        kv = self.kernel
        return (200, {
            "version": __version__, "leader": kv.leader.is_leader(),
            "holder": kv.config.leader_holder, "store": type(kv.store).__name__,
            "writes": kv.store.write_count(),
            "workflows": len(kv.store.snapshot().workflows),
            "workloads": len(kv.store.snapshot().workloads),
            "nodes": len(kv.store.snapshot().nodes),
            "leases": len(kv.store.snapshot().leases),
            "loop_errors": kv._loop_errors,
        }, {"Cache-Control": "no-store"})

    def _metrics(self, h, p, q, t):
        return (200, {"metrics": self.kernel.metrics.snapshot()}, {})

    def _me(self, h, p, q, t):
        claims = self._auth_for(h)
        return (200, {"claims": {"namespace": claims.namespace, "scopes": sorted(claims.scopes),
                                 "expires_at": claims.expires_at}}, {})

    def _create_namespace(self, h, p, q, t):
        actor = "bootstrap-admin" if getattr(h, "_bootstrap", False) else \
            self._auth_for(h, scope="admin").token_id
        body = h._body()
        ns = self.kernel.write(lambda: self.kernel.create_namespace(body["name"], actor))
        return (200, {"namespace": ns.name, "created_at": ns.created_at}, {})

    def _issue_token(self, h, p, q, t):
        actor = "bootstrap-admin" if getattr(h, "_bootstrap", False) else \
            self._auth_for(h, scope="admin").token_id
        body = h._body()
        token, raw = self.kernel.issue_token(body["namespace"], body.get("scopes", ["read"]),
                                             actor=actor)
        return (200, {"token_id": token.id, "token": raw, "scopes": token.scope_list,
                      "expires_at": token.expires_at}, {})

    def _audit(self, h, p, q, t):
        claims = self._auth_for(h, scope="admin")
        limit = int(q.get("limit", "100"))
        entries = list(reversed(self.kernel.store.snapshot().audit))[: min(limit, 1000)]
        return (200, {"audit": entries}, {})

    def _create_workload(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        body_out = self._idempotent(
            h, t, body,
            lambda: self.kernel.create_workload(claims.namespace, body, actor=claims.token_id),
            lambda wl: {"workload": to_jsonable(wl)},
        )
        return (200, body_out, {})

    def _list_workloads(self, h, p, q, t):
        claims = self._auth_for(h)
        snap = self.kernel.store.snapshot()
        items = [wl for wl in snap.workloads.values() if wl.namespace == claims.namespace]
        return (200, {"workloads": [to_jsonable(w) for w in sorted(items, key=lambda x: x.created_at)]}, {})

    def _get_workload(self, h, p, q, t):
        claims = self._auth_for(h)
        wl = self._require_workload(p["wid"], claims.namespace)
        return (200, {"workload": to_jsonable(wl)}, {})

    def _delete_workload(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        self._require_workload(p["wid"], claims.namespace)
        self.kernel.write(lambda: (self.kernel.store.delete_workload(p["wid"]),
                                   self.kernel._audit(claims.token_id, "workload.delete", p["wid"], "ok")))
        return (200, {"ok": True}, {})

    def _scale_workload(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        self._require_workload(p["wid"], claims.namespace)
        body = h._body()
        desired = int(body["replicas"])
        wl = self.kernel.write(lambda: self.kernel.scale_workload(p["wid"], desired,
                                                                  actor=claims.token_id))
        return (200, {"workload": to_jsonable(wl)}, {})

    def _ingest_metrics(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        self._require_workload(p["wid"], claims.namespace)
        body = h._body()
        self.kernel.ingest_metrics(p["wid"], body.get("metrics", body))
        return (200, {"ok": True}, {})

    def _create_node(self, h, p, q, t):
        actor = "bootstrap-admin" if getattr(h, "_bootstrap", False) else \
            self._auth_for(h, scope="admin").token_id
        body = h._body()

        def op():
            from ..domain.models import Node

            node = Node.create(body, self.kernel.clock.now())
            self.kernel.store.put_node(node)
            self.kernel._audit(actor, "node.create", node.id, "ok", {"name": node.name})
            return node
        node = self.kernel.write(op)
        return (200, {"node": to_jsonable(node)}, {})

    def _list_nodes(self, h, p, q, t):
        self._auth_for(h)
        nodes = sorted(self.kernel.store.snapshot().nodes.values(), key=lambda x: x.name)
        return (200, {"nodes": [to_jsonable(n) for n in nodes]}, {})

    def _cordon_node(self, h, p, q, t):
        actor = "bootstrap-admin" if getattr(h, "_bootstrap", False) else \
            self._auth_for(h, scope="admin").token_id
        body = h._body()

        def op():
            node = self._node_copy(p["nid"])
            if node is None:
                return None
            if bool(body.get("cordon", True)):
                node.state = NodeState.CORDONED
            else:
                node.state = NodeState.ACTIVE
            self.kernel.store.put_node(node)
            self.kernel._audit(actor, "node.cordon", node.id, "ok",
                               {"state": node.state.value})
            return node
        node = self.kernel.write(op)
        if node is None:
            from ..domain.errors import NotFoundError
            raise NotFoundError(f"node {p['nid']} not found")
        return (200, {"node": to_jsonable(node)}, {})

    def _drain_node(self, h, p, q, t):
        actor = "bootstrap-admin" if getattr(h, "_bootstrap", False) else \
            self._auth_for(h, scope="admin").token_id

        def op():
            node = self._node_copy(p["nid"])
            if node is None:
                return None
            node.state = NodeState.DRAINING
            self.kernel.store.put_node(node)
            self.kernel._audit(actor, "node.drain", node.id, "ok", {})
            return node
        node = self.kernel.write(op)
        if node is None:
            from ..domain.errors import NotFoundError
            raise NotFoundError(f"node {p['nid']} not found")
        return (200, {"node": to_jsonable(node)}, {})

    def _create_workflow(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        body_out = self._idempotent(
            h, t, body,
            lambda: self.kernel.engine.submit(claims.namespace, body, trace_id=t),
            lambda wf: {"workflow": self._wf_json(wf)},
        )
        return (200, body_out, {})

    def _list_workflows(self, h, p, q, t):
        claims = self._auth_for(h)
        snap = self.kernel.store.snapshot()
        items = [w for w in snap.workflows.values() if w.namespace == claims.namespace]
        return (200, {"workflows": [self._wf_json(w) for w in sorted(items, key=lambda x: x.created_at)]}, {})

    def _get_workflow(self, h, p, q, t):
        claims = self._auth_for(h)
        wf = self._require_workflow(p["wid"], claims.namespace)
        return (200, {"workflow": self._wf_json(wf)}, {})

    def _cancel_workflow(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        # cancel is a single writer op: engine.cancel mutates via store directly
        wf = self.kernel.write(lambda: self.kernel.engine.cancel(p["wid"], trace_id=t))
        wf = self._require_workflow(p["wid"], claims.namespace)
        return (200, {"workflow": self._wf_json(wf),
                      "note": "running attempts are revoked; completion is idempotent"}, {})

    def _retry_node(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        self._require_workflow(p["wid"], claims.namespace)
        self.kernel.write(lambda: self.kernel.engine.retry_node(p["wid"], p["nid"], trace_id=t))
        wf = self._require_workflow(p["wid"], claims.namespace)
        return (200, {"workflow": self._wf_json(wf)}, {})

    def _list_attempts(self, h, p, q, t):
        claims = self._auth_for(h)
        claimable = q.get("claimable")
        claimable_flag = None if claimable is None else str(claimable).lower() in ("1", "true", "yes")
        try:
            limit = int(q.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        items = self.kernel.engine.attempts_view(
            namespace=claims.namespace, state=q.get("state") or q.get("status"),
            claimable=claimable_flag, lease_id=q.get("lease_id"),
            workflow_id=q.get("workflow_id"), worker_id=q.get("worker_id"), limit=limit)
        return (200, {"attempts": items, "count": len(items)}, {})

    def _get_attempt(self, h, p, q, t):
        claims = self._auth_for(h)
        wf = None
        snap = self.kernel.store.snapshot()
        attempt = snap.attempts.get(p["aid"])
        if attempt is not None:
            wf = snap.workflow(attempt.workflow_id)
        if attempt is None or wf is None or wf.namespace != claims.namespace:
            from ..domain.errors import NotFoundError
            raise NotFoundError(f"attempt not found: {p['aid']}")
        return (200, {"attempt": self.kernel.engine.attempt_view(attempt, snap)}, {})

    def _claim_attempt(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        worker_id = str(body.get("worker_id") or "").strip()
        ttl = body.get("ttl_seconds")

        def op():
            # claim + audit in the same write: single-writer discipline (ADR-005)
            view = self.kernel.engine.claim_attempt(
                p["aid"], worker_id=worker_id, namespace=claims.namespace,
                ttl=float(ttl) if ttl else None, trace_id=t)
            self.kernel._audit(worker_id, "attempt.claim", p["aid"], "ok",
                               {"lease": view.get("lease_id"),
                                "workflow": view.get("workflow_id")}, t)
            return view

        view = self.kernel.write(op)
        return (200, {"attempt": view, "nonce": view["nonce"],
                      "lease_id": view["lease_id"], "deadline": view["deadline"]}, {})

    def _heartbeat_attempt(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        ttl = body.get("ttl_seconds")
        out = self.kernel.write(lambda: self.kernel.engine.heartbeat_attempt(
            p["aid"], worker_id=str(body.get("worker_id") or ""), nonce=str(body.get("nonce") or ""),
            namespace=claims.namespace, ttl=float(ttl) if ttl else None, trace_id=t))
        return (200, {"ok": True, **out}, {})

    def _complete_attempt(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        result = body.get("result")
        if result is not None:
            from ..domain.errors import ValidationError as _V
            if not isinstance(result, dict):
                raise _V("result must be a JSON object")
            encoded = json.dumps(result, default=str)
            if len(encoded.encode()) > self.kernel.config.attempt_result_max_bytes:
                raise _V(f"result exceeds {self.kernel.config.attempt_result_max_bytes} bytes")
        actor = str(body.get("worker_id") or claims.token_id)

        def op():
            out = self.kernel.engine.attempt_complete(
                p["aid"], ok=bool(body.get("ok", True)), error=body.get("error"),
                trace_id=t, namespace=claims.namespace,
                worker_id=str(body.get("worker_id") or "") or None,
                nonce=body.get("nonce"), result=result)
            if out and not out.get("idempotent"):
                self.kernel._audit(actor, "attempt.complete", p["aid"], "ok",
                                   {"workflow": out.get("workflow_id"),
                                    "node": out.get("node_id"),
                                    "status": out.get("status"),
                                    "ok": bool(body.get("ok", True))}, t)
                self.kernel.metrics.counter("worker.completions").inc()
            return out

        out = self.kernel.write(op)
        return (200, {"ok": True, **(out or {})}, {})

    def _list_leases(self, h, p, q, t):
        claims = self._auth_for(h)
        snap = self.kernel.store.snapshot()
        leases = [l for l in snap.leases.values() if l.namespace == claims.namespace]
        return (200, {"leases": [to_jsonable(l) for l in sorted(leases, key=lambda x: x.created_at)]}, {})

    def _publish_event(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()

        def op():
            ev = self.kernel.bus.publish(body["topic"], body.get("key", "default"),
                                         body.get("payload", {}),
                                         schema_version=int(body.get("schema_version", 1)),
                                         trace_id=t)
            self.kernel._audit(claims.token_id, "event.publish", ev.id, "ok",
                               {"topic": ev.topic, "partition": ev.partition, "seq": ev.seq})
            return ev
        ev = self.kernel.write(op)
        return (200, {"event": to_jsonable(ev)}, {})

    def _fetch_events(self, h, p, q, t):
        claims = self._auth_for(h)
        group = q.get("group", "default")
        limit = min(int(q.get("limit", "10")), 100)
        events = self.kernel.bus.next_batch(p["topic"], group, limit=limit)
        return (200, {"events": [to_jsonable(e) for e in events]}, {})

    def _ack_event(self, h, p, q, t):
        claims = self._auth_for(h, scope="write")
        body = h._body()
        group = body.get("group", "default")
        self.kernel.write(lambda: self.kernel.bus.ack(
            p["topic"], group, body["event_id"], ok=bool(body.get("ok", True)),
            error=body.get("error"), consumer=claims.token_id))
        return (200, {"ok": True}, {})

    def _list_dlq(self, h, p, q, t):
        self._auth_for(h)
        snap = self.kernel.store.snapshot()
        entries = sorted([e for e in snap.dlqs.values() if e.topic == p["topic"]],
                         key=lambda e: e.created_at)
        return (200, {"dead_letters": [to_jsonable(e) for e in entries]}, {})

    # ---------------------------------------------------------------- helpers

    def _require_workload(self, wid: str, namespace: str):
        wl = self.kernel.store.snapshot().workload(wid)
        if wl is None:
            from ..domain.errors import NotFoundError
            raise NotFoundError(f"workload {wid} not found")
        if wl.namespace != namespace:
            raise Forbidden("workload belongs to another namespace")
        return wl

    def _require_workflow(self, wid: str, namespace: str):
        wf = self.kernel.store.snapshot().workflow(wid)
        if wf is None:
            from ..domain.errors import NotFoundError
            raise NotFoundError(f"workflow {wid} not found")
        if wf.namespace != namespace:
            raise Forbidden("workflow belongs to another namespace")
        return wf

    def _node_copy(self, nid: str):
        import copy

        node = self.kernel.store.snapshot().node(nid)
        return copy.deepcopy(node) if node else None

    def _wf_json(self, wf) -> dict:
        from ..domain.models import to_jsonable

        snap = self.kernel.store.snapshot()
        attempts = [self.kernel.engine.attempt_view(a, snap)
                    for a in snap.attempts.values() if a.workflow_id == wf.id]
        attempts.sort(key=lambda a: (a["node_id"], a["attempt_no"]))
        return {
            **to_jsonable(wf),
            "nodes": [to_jsonable(n) for n in wf.nodes.values()],
            "attempts": attempts,
        }

    def _idempotent(self, h, trace_id: str, body: dict, fn: Callable,
                    responder: Callable[[Any], dict]):
        key = h.headers.get("Idempotency-Key") or h.headers.get("X-Idempotency-Key")
        if not key:
            return responder(fn())
        payload = json.dumps({"method": h.command, "path": h.path, "body": body},
                             sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode()).hexdigest()

        def op():
            snap = self.kernel.store.snapshot()
            existing = snap.idem_response.get(key)
            if existing is not None:
                stored_body, status, _ = existing
                if snap.idempotency.get(key) != digest:
                    from ..domain.errors import ConflictError
                    raise ConflictError("Idempotency-Key reused with a different payload")
                return ("replay", stored_body, status)
            result = fn()
            response_body = responder(result)
            self.kernel.store.put_idempotency(key, digest, 200, response_body)
            return ("fresh", response_body, 200)

        kind, stored_body, status = self.kernel.write(op)
        if kind == "replay":
            raise _Replay(stored_body, status)
        return stored_body

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._registered()
        host, _, port = self.kernel.config.listen.rpartition(":")
        handler = type("BoundHandler", (ApiHandler,), {
            "kernel": self.kernel, "limiter": self.limiter, "routes": self.routes,
        })
        self._httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="es-api", daemon=True)
        self._thread.start()
        self.kernel.logger.log("api.listening", info=True, listen=self.kernel.config.listen)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()


class _Replay(Exception):
    def __init__(self, body: dict, status: int):
        super().__init__(f"idempotent replay ({status})")
        self.body = body
        self.status = status


class IdempotentReplayHandled:
    """Marker: _Replay is caught in ApiServer.launch wrapper."""
    pass


def launch(kernel: Kernel) -> ApiServer:
    server = ApiServer(kernel)
    server.start()
    return server
