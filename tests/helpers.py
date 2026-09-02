"""Test doubles + harness helpers (deterministic, no external services)."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from effective_scale.adapters import MemoryStore, SQLiteStore  # noqa: E402
from effective_scale.api.server import ApiServer  # noqa: E402
from effective_scale.core.kernel import Config, Kernel  # noqa: E402


class FakeClock:
    """Deterministic wall clock for pure-function tests (no sleeping)."""

    def __init__(self, start: float = 1_700_000_000.0):
        self._now = start
        self._mono = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self._mono += seconds


class Harness:
    """Kernel + API on an ephemeral port, with an auto-issued admin token."""

    def __init__(self, store=None, *, config: Config | None = None, start_api: bool = True):
        cfg = config or Config(store_path=":memory:", listen="127.0.0.1:0",
                               auth_secret="test-secret-0123456789abcdef",
                               admin_token="bootstrap-test-token",
                               scheduler_interval=0.05, scale_interval=0.05,
                               workflow_interval=0.02, event_interval=0.02,
                               heartbeat_ttl=1.0, watchdog_stall=30.0)
        from effective_scale.ports.logger import MemLogger

        self.kernel = Kernel(store or MemoryStore(), config=cfg, logger=MemLogger())
        self.kernel.start()
        self.api: ApiServer | None = None
        if start_api:
            self.api = ApiServer(self.kernel)
            self.api.start()  # start() registers routes once
            self.port = self.api._httpd.server_address[1]  # type: ignore[union-attr]

    def stop(self) -> None:
        if self.api:
            self.api.stop()
        self.kernel.stop()

    # -- HTTP ---------------------------------------------------------------
    def request(self, method: str, path: str, body: dict | None = None,
                token: str | None = None, admin: bool = False,
                headers: dict | None = None) -> tuple[int, dict, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body or {}).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if admin:
            hdrs["X-Admin-Token"] = self.kernel.config.admin_token
        for k, v in (headers or {}).items():
            hdrs[k] = v
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode() or "{}")
        out_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, data, out_headers

    def admin_token(self) -> str:
        status, data, _ = self.request("POST", "/v1/tokens",
                                       {"namespace": "demo", "scopes": ["read", "write", "admin"]},
                                       admin=True)
        assert status == 200, data
        return data["token"]


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
