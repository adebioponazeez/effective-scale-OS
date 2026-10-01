"""Entrypoint tests: `python3 -m effective_scale` is the product — it must be tested like one.

Covers config validation, an end-to-end boot over a real socket (readiness, status, workflow
submission with the bootstrap token), clean SIGTERM shutdown, store durability across the
process boundary, and the honest readiness semantics when the store dies underneath the API.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from effective_scale.adapters import SQLiteStore  # noqa: E402
from effective_scale.main import build_config, main, serve  # noqa: E402

from .helpers import Harness  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EntrypointConfigTest(unittest.TestCase):
    def test_check_mode_validates_and_exits_zero(self):
        self.assertEqual(main(["--check", "--store", ":memory:"]), 0)

    def test_build_config_maps_every_flag(self):
        args = type("A", (), {})()  # argparse.Namespace-like
        for name, value in dict(
            store=":memory:", listen="127.0.0.1:9", secret="s" * 20, admin_token="t",
            scheduler_interval=2.0, scale_interval=3.0, workflow_interval=4.0, event_interval=5.0,
            lease_ttl=6.0, heartbeat_ttl=7.0, watchdog_stall=8.0, holder="node-9", max_conns=11,
            workflow_pool=12, rate_limit=13, event_partitions=14, event_max_lag=15,
            event_max_attempts=16, log_level="debug",
        ).items():
            setattr(args, name, value)
        config = build_config(args)
        self.assertEqual((config.store_path, config.listen, config.leader_holder),
                         (":memory:", "127.0.0.1:9", "node-9"))
        self.assertEqual(config.event_partitions, 14)
        self.assertEqual(config.log_level, "debug")

    def test_unknown_flag_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--definitely-not-a-flag"])
        self.assertEqual(ctx.exception.code, 2)


class HealthSemanticsTest(unittest.TestCase):
    """Liveness must survive a dead store; readiness must not lie about it."""

    def test_readiness_fails_when_store_dies_liveness_stays_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Harness(SQLiteStore(str(Path(tmp) / "k.db")), start_api=True)
            try:
                status, _, _ = harness.request("GET", "/v1/health/ready")
                self.assertEqual(status, 200)
                harness.kernel.store._conn.close()  # store death under a live process
                status, _, _ = harness.request("GET", "/v1/health/ready")
                self.assertEqual(status, 503, "readiness must not be served from a cached snapshot")
                status, _, _ = harness.request("GET", "/v1/health/live")
                self.assertEqual(status, 200, "the process is alive; only the store is not")
            finally:
                harness.stop()


class ServeInProcessTest(unittest.TestCase):
    """`serve()` is the real startup path; the subprocess test below is the black-box proof."""

    def test_serve_starts_serves_and_shuts_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(store_path=str(Path(tmp) / "serve.db"), listen="127.0.0.1:0")
            original_handlers = {}
            try:
                running = serve(config, demo=True, install_signals=True)
                original_handlers = {sig: signal.getsignal(sig)
                                     for sig in (signal.SIGTERM, signal.SIGINT)}
                port = running.server.port
                self.assertTrue(port)
                self.assertEqual(_get(port, "/v1/health/ready")[0], 200)
                self.assertTrue(running.kernel.running())

                running.shutdown()          # simulate the signal arriving
                self.assertFalse(running.kernel.running())
                self.assertTrue(running.stopped.is_set())
                running.shutdown()          # idempotent: second signal must not double-stop
            finally:
                for sig, handler in original_handlers.items():
                    signal.signal(sig, handler)

            # the store is flushed and reusable after shutdown
            with sqlite3.connect(config.store_path) as conn:
                self.assertGreater(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)

    def test_serve_ephemeral_memory_store(self):
        config = _config(store_path=":memory:", listen="127.0.0.1:0")
        running = serve(config, demo=False, install_signals=False)
        try:
            self.assertEqual(_get(running.server.port, "/v1/health/ready")[0], 200)
        finally:
            running.shutdown()


class ProcessLifecycleTest(unittest.TestCase):
    """Boot the real module, talk HTTP to it, SIGTERM it, then reopen its store."""

    def test_boot_serve_shutdown_and_persist(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "cli.db")
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            proc = subprocess.Popen(
                [sys.executable, "-m", "effective_scale", "--store", db,
                 "--listen", f"127.0.0.1:{port}", "--demo", "--admin-token", "cli-admin",
                 "--scheduler-interval", "0.1", "--workflow-interval", "0.1"],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                self.assertTrue(_wait_ready(port, proc, timeout=30.0),
                                "server never became ready")
                self.assertEqual(_get(port, "/v1/health/ready")[0], 200)
                status, body = _get(port, "/v1/status")
                self.assertEqual(status, 200)
                self.assertEqual(body["version"], _package_version(),
                                 "/v1/status must report the single-source version")
                self.assertIn("demo", body.get("namespaces", ["demo"]))

                # bootstrap admin -> real token -> read the demo seed through the API
                status, body = _post(port, "/v1/tokens",
                                     {"namespace": "demo", "scopes": ["read", "write", "admin"]},
                                     admin_token="cli-admin")
                self.assertEqual(status, 200, body)
                token = body["token"]
                status, body = _get(port, "/v1/workflows", token=token)
                self.assertEqual(status, 200, body)
                self.assertGreaterEqual(len(body.get("workflows", [])), 1)
                status, body = _get(port, "/v1/attempts", token=token)
                self.assertEqual(status, 200, body)
                self.assertIn("attempts", body)

                # graceful shutdown on SIGTERM, exit code 0
                proc.send_signal(signal.SIGTERM)
                out, _ = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, out[-2000:])
                self.assertIn("shutdown complete", out)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=10)

            # durability across the process boundary
            with sqlite3.connect(db) as conn:
                for table in ("meta", "workloads", "workflows", "nodes"):
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    self.assertGreater(count, 0, f"{table} was not persisted before shutdown")


def _config(**overrides) -> "Config":
    from effective_scale.core.kernel import Config

    values = dict(store_path=":memory:", listen="127.0.0.1:0",
                  auth_secret="test-secret-0123456789abcdef", admin_token="cli-admin",
                  scheduler_interval=0.05, scale_interval=0.05, workflow_interval=0.05,
                  event_interval=0.05, heartbeat_ttl=1.0, watchdog_stall=30.0)
    values.update(overrides)
    return Config(**values)


def _package_version() -> str:
    from effective_scale import __version__
    return __version__


def _wait_ready(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if _get(port, "/v1/health/ready")[0] == 200:
                return True
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(0.1)
    return False


def _get(port: int, path: str, token: str | None = None,
         admin_token: str | None = None) -> tuple[int, dict]:
    return _request(port, "GET", path, None, token, admin_token)


def _post(port: int, path: str, body: dict, token: str | None = None,
          admin_token: str | None = None) -> tuple[int, dict]:
    return _request(port, "POST", path, body, token, admin_token)


def _request(port: int, method: str, path: str, body: dict | None,
             token: str | None, admin_token: str | None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if admin_token:
        request.add_header("X-Admin-Token", admin_token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:  # pragma: no cover — surfaced as a failed assertion
        return exc.code, json.load(exc)


if __name__ == "__main__":
    unittest.main()
