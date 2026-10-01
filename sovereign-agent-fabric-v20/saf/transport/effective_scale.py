"""Optional integration: SAF → effective-scale-OS (workload kernel).

Architecture mapping (docs/01-final-system-architecture.md):
  SAF decides WHAT — intent → capabilities → ranked resources (resolver).
  effective-scale-OS provides the durable execution-plane record: idempotent
  workflow submission, per-node retry/timeout policy, observed state, event
  trace and audit trail (kernel's docs/09-api-reference.md).

Each required capability becomes one workflow node (`exec.topic` = fire on
durable publish, no lease needed), so the *plan* is durably recorded and
observable without pretending a client-side worker can complete lease-bound
attempts through the public API.

This module is import-safe without effective-scale-OS installed: it uses only
the standard library and fails with a structured `TransportError` when the
kernel is unreachable (offline first-class, docs §21).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import urllib.error
import urllib.request

from saf.core.contracts import Task


class TransportError(RuntimeError):
    """Structured transport failure (unreachable kernel, auth, validation)."""

    def __init__(self, message: str, *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}

    @property
    def code(self) -> str:
        return (self.body.get("error") or {}).get("code", "transport_error")


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def idempotency_key(task: Task) -> str:
    """Deterministic key: same intent + capability set → same workflow (replay-safe)."""
    digest = hashlib.sha256(
        json.dumps(
            {"intent": task.intent, "capabilities": sorted(task.required_capabilities)},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"saf-{digest[:40]}"


class EffectiveScaleTransport:
    """Thin, injectable HTTP client for the effective-scale-OS v1 API.

    `client` is an injectable callable `(method, url, body, headers) ->
    (status, parsed_json)` for tests; the default implementation uses
    `urllib` in a worker thread (never blocks the event loop).
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        namespace: str = "default",
        client=None,
        timeout_s: float = 30.0,
        node_timeout_s: float = 300.0,
        retry_max: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self._client = client or self._default_client
        self.timeout_s = timeout_s
        self.node_timeout_s = node_timeout_s
        self.retry_max = retry_max

    # ------------------------------------------------------------------ API

    async def submit(self, task: Task, *, workload_id: str | None = None,
                     idem_key: str | None = None) -> dict:
        """Durably record the capability plan as an idempotent workflow.

        Two node shapes are supported:
          * `workload_id` -> lease-bound nodes a SAF worker claims and completes
            through the external worker protocol (ADR-006) — real execution;
          * no workload  -> fire-and-continue event nodes (the plan is recorded
            and observable, but nothing executes it) — the original V0 contract.
        """
        caps = task.required_capabilities or ["cap://general/agent/execute"]
        # The capability plan is a chain in declared order: capability i+1
        # depends on i. Without this, a worker pool could run `run tests`
        # before `refactor` — the plan would be recorded but not respected.
        nodes = [
            {
                "id": f"cap-{i}",
                "name": cap,
                "depends_on": [f"cap-{i - 1}"] if i else [],
                "exec": ({"workload_id": workload_id} if workload_id
                         else {"topic": f"saf.task.{slug(cap)}"}),
                "timeout": self.node_timeout_s,
                "retry": {
                    "max": self.retry_max,
                    "base_seconds": 1.0,
                    "max_seconds": 60.0,
                    "jitter": 0.2,
                },
                "max_concurrency": 16,
            }
            for i, cap in enumerate(caps)
        ]
        workflow = {
            "name": f"saf/{slug(task.intent)[:48] or 'task'}",
            "nodes": nodes,
            "timeout": self.node_timeout_s * max(1, len(nodes)),
            "dead_letter": True,
        }
        key = idem_key or idempotency_key(task)
        status, body = await self._request(
            "POST", "/v1/workflows", body=workflow, idempotency_key=key
        )
        if status != 200:
            raise TransportError(
                f"workflow submission failed ({status})", status=status, body=body
            )
        return {
            "workflow": body["workflow"],
            "idempotency_key": key,
            "replayed": bool(body.get("replayed")),
            "namespace": self.namespace,
        }

    async def status(self, workflow_id: str) -> dict:
        status, body = await self._request("GET", f"/v1/workflows/{workflow_id}")
        if status != 200:
            raise TransportError(f"workflow status failed ({status})", status=status, body=body)
        return body.get("workflow", body)

    # ------------------------------------------- external worker protocol (ADR-006)

    async def list_attempts(self, *, claimable: bool | None = None, state: str | None = "running",
                            workflow_id: str | None = None, limit: int = 10) -> list[dict]:
        """Discover work. `claimable=True` returns only unclaimed lease-bound attempts."""
        query = [f"limit={int(limit)}"]
        if claimable is not None:
            query.append(f"claimable={'true' if claimable else 'false'}")
        if state:
            query.append(f"state={state}")
        if workflow_id:
            query.append(f"workflow_id={workflow_id}")
        status, body = await self._request("GET", f"/v1/attempts?{'&'.join(query)}")
        if status != 200:
            raise TransportError(f"attempt list failed ({status})", status=status, body=body)
        return list(body.get("attempts", []))

    async def claim_attempt(self, attempt_id: str, *, worker_id: str,
                            ttl_seconds: float | None = None) -> dict:
        payload = {"worker_id": worker_id}
        if ttl_seconds:
            payload["ttl_seconds"] = float(ttl_seconds)
        status, body = await self._request("POST", f"/v1/attempts/{attempt_id}/claim", body=payload)
        if status != 200:
            raise TransportError(f"claim failed ({status})", status=status, body=body)
        return body

    async def heartbeat_attempt(self, attempt_id: str, *, worker_id: str, nonce: str,
                                ttl_seconds: float | None = None) -> dict:
        payload = {"worker_id": worker_id, "nonce": nonce}
        if ttl_seconds:
            payload["ttl_seconds"] = float(ttl_seconds)
        status, body = await self._request("POST", f"/v1/attempts/{attempt_id}/heartbeat",
                                           body=payload)
        if status != 200:
            raise TransportError(f"heartbeat failed ({status})", status=status, body=body)
        return body

    async def complete_attempt(self, attempt_id: str, *, ok: bool, worker_id: str,
                               nonce: str, error: str | None = None,
                               result: dict | None = None) -> dict:
        payload: dict = {"ok": bool(ok), "worker_id": worker_id, "nonce": nonce}
        if error:
            payload["error"] = error
        if result is not None:
            payload["result"] = result
        status, body = await self._request("POST", f"/v1/attempts/{attempt_id}/complete",
                                           body=payload)
        if status != 200:
            raise TransportError(f"completion rejected ({status})", status=status, body=body)
        return body

    async def health(self) -> dict:
        status, body = await self._request("GET", "/v1/health/ready")
        if status != 200:
            raise TransportError(f"kernel not ready ({status})", status=status, body=body)
        return body

    # ------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await asyncio.to_thread(self._client, method, url, body, headers)

    def _default_client(
        self, method: str, url: str, body: dict | None, headers: dict
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                parsed = {}
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"effective-scale-OS unreachable: {exc}") from exc
        try:
            return 200, json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise TransportError(f"malformed response from kernel: {exc}") from exc
