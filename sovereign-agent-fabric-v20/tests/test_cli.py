"""CLI entrypoint tests: `saf ...` is the operator surface — every branch must behave.

These are the tests that were missing entirely (saf/cli/main.py had 0% coverage): the
dispatch table, the honest failure modes when the kernel is unreachable, the offline
outbox, and a full execute -> evidence -> verify lifecycle on a temp workspace.
"""
import json
import sys

import pytest

from saf.cli import main as cli

DEAD_KERNEL = "http://127.0.0.1:1"  # nothing listens here; connection refused immediately


def run(monkeypatch, capsys, *argv):
    """Invoke the CLI as `saf <argv>`; returns (exit_code, parsed stdout JSON or None)."""
    monkeypatch.setattr(sys, "argv", ["saf", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out.strip()
    payload = None
    if out.startswith("{"):
        payload = json.loads(out)
    return code, payload


def test_doctor_reports_platform(monkeypatch, capsys):
    code, payload = run(monkeypatch, capsys, "doctor")
    assert code == 0 and payload["status"] == "ok"
    assert payload["python"].startswith("3.")


def test_capabilities_lists_registered_resources(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["saf", "capabilities"])
    cli.main()  # prints lines, not JSON — read the buffer directly
    out = capsys.readouterr().out
    assert "cap://software/testing/execute" in out
    # the CLI adapters must really claim what the ontology says they claim (C-caps)
    assert out.count("cap://general/agent/execute") >= 5


def test_resources_with_stats(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "resources", "--stats",
                        "--state-dir", str(tmp_path))
    assert code == 0
    assert len(payload["resources"]) >= 5
    assert "observed" in payload


def test_no_subcommand_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["saf"])
    cli.main()
    assert "usage" in capsys.readouterr().out.lower()


def test_run_offline_queues_in_outbox(monkeypatch, capsys, tmp_path):
    """Kernel unreachable + queueing on (default) => queued_offline with an outbox id."""
    code, payload = run(monkeypatch, capsys, "run", "refactor the repository",
                        "--es", DEAD_KERNEL, "--state-dir", str(tmp_path))
    assert code == 0
    plan = payload["execution_plan"]
    assert plan["status"] == "queued_offline"
    assert plan["outbox_id"]
    assert (tmp_path / "outbox.jsonl").exists()


def test_run_offline_without_queue_is_explicit(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "run", "refactor the repository",
                        "--es", DEAD_KERNEL, "--no-queue", "--state-dir", str(tmp_path))
    assert code == 0
    plan = payload["execution_plan"]
    assert plan["status"] == "unavailable"
    assert plan["code"] in {"connection_refused", "transport_error", "unreachable", "network"}
    assert not (tmp_path / "outbox.jsonl").exists()


def test_run_without_kernel_still_ranks_candidates(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "run", "run the tests",
                        "--state-dir", str(tmp_path))
    assert code == 0 and payload["status"] == "ok"
    assert payload["capabilities"] == ["cap://software/testing/execute"]
    assert payload["ranked_candidates"]


def test_sync_reports_kernel_unavailable_with_outbox_intact(monkeypatch, capsys, tmp_path):
    run(monkeypatch, capsys, "run", "refactor", "--es", DEAD_KERNEL, "--state-dir", str(tmp_path))
    code, payload = run(monkeypatch, capsys, "sync", "--es", DEAD_KERNEL,
                        "--state-dir", str(tmp_path))
    assert code == 1, payload
    assert payload["status"] == "kernel_unavailable"  # non-zero: a script must notice
    assert payload["outbox"]["pending"] == 1          # never silently dropped
    assert len(payload["deferred"]) == 1 and payload["deferred"][0]["id"]


def test_verify_empty_ledger_is_ok(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "verify", "--state-dir", str(tmp_path))
    assert code == 0 and payload["ok"] is True and payload["records"] == 0


def test_ledger_empty_state(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "ledger", "--state-dir", str(tmp_path))
    assert code == 0 and payload["count"] == 0


def test_rollback_with_no_points_is_honest(monkeypatch, capsys, tmp_path):
    code, payload = run(monkeypatch, capsys, "rollback", "latest",
                        "--state-dir", str(tmp_path), "--dry-run")
    assert code == 1 and payload["error"] == "no rollback points"


def test_execute_unimplemented_capability_exits_nonzero(monkeypatch, capsys, tmp_path):
    """git/operate is a declared-but-unimplemented capability (tracked in known-gaps.json):
    the CLI must fail loudly, never pretend the work happened."""
    code, payload = run(monkeypatch, capsys, "execute", "git commit the changes",
                        "--workspace", str(tmp_path), "--state-dir", str(tmp_path / ".saf"))
    assert code == 2
    assert payload["ok"] is False


def test_execute_tests_then_verify_ledger(monkeypatch, capsys, tmp_path):
    """Full operator lifecycle: execute a real capability, record evidence, verify the chain."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "app.py").write_text("VALUE = 1\n")
    state = tmp_path / ".saf"
    code, payload = run(monkeypatch, capsys, "execute", "run the tests",
                        "--workspace", str(workspace), "--state-dir", str(state),
                        "--test-command", "python3 -c pass")
    assert code == 0, payload
    assert payload["ok"] is True
    assert payload["execution_id"]

    code, report = run(monkeypatch, capsys, "verify", "--state-dir", str(state))
    assert code == 0 and report["ok"] is True and report["records"] >= 1

    code, ledger = run(monkeypatch, capsys, "ledger", "--state-dir", str(state))
    assert code == 0 and ledger["count"] >= 1
