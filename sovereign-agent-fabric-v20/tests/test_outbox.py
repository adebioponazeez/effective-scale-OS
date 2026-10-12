"""Offline outbox (§21): persist -> sync -> acknowledge -> reconcile."""
import asyncio
import json

from saf.transport.effective_scale import TransportError
from saf.transport.outbox import Outbox, payload_hash, reconcile


def test_outbox_lifecycle_and_summary(tmp_path):
    box = Outbox(str(tmp_path / "outbox.jsonl"))
    entry = box.enqueue("workflow.submit", {"intent": "run tests", "namespace": "demo"})
    assert entry["status"] == "pending" and entry["payload_hash"] == payload_hash(entry["payload"])
    assert box.summary() == {"total": 1, "by_status": {"pending": 1}, "pending": 1}

    box.note_attempt(entry["id"], "connection refused")
    assert box.get(entry["id"])["attempts"] == 1
    box.ack(entry["id"], {"workflow": {"id": "wf-1"}})
    latest = box.get(entry["id"])
    assert latest["status"] == "acked" and latest["result"]["workflow"]["id"] == "wf-1"
    assert box.pending() == []


def test_outbox_survives_a_torn_tail(tmp_path):
    path = tmp_path / "outbox.jsonl"
    box = Outbox(str(path))
    box.enqueue("workflow.submit", {"intent": "a"})
    with open(path, "ab") as f:
        f.write(b'{"id": "ob-torn", "statt')  # crash mid-append
    entries = box.entries()
    assert len(entries) == 1 and entries[0]["payload"]["intent"] == "a"


class StubTransport:
    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls = []

    async def submit(self, task, *, workload_id=None, idem_key=None):
        self.calls.append({"intent": task.intent, "workload_id": workload_id, "idem_key": idem_key})
        if self.behaviour == "offline":
            raise TransportError("kernel unreachable")
        return {"workflow": {"id": f"wf-{len(self.calls)}", "status": "running"},
                "idempotency_key": idem_key, "replayed": self.behaviour == "replay"}


def test_reconcile_acks_and_uses_payload_hash_as_idempotency_key(tmp_path):
    box = Outbox(str(tmp_path / "outbox.jsonl"))
    entry = box.enqueue("workflow.submit", {"intent": "refactor repository", "namespace": "demo"})
    transport = StubTransport()
    summary = asyncio.run(reconcile(box, transport))

    assert len(summary["acked"]) == 1 and summary["deferred"] == []
    assert transport.calls[0]["idem_key"] == entry["payload_hash"]
    assert box.get(entry["id"])["status"] == "acked"
    assert box.get(entry["id"])["result"]["workflow"]["id"] == "wf-1"

    # a second sync has nothing to do — no duplicate submissions
    again = asyncio.run(reconcile(box, transport))
    assert again["checked"] == 0 and len(transport.calls) == 1


def test_reconcile_defers_when_offline_and_retries_later(tmp_path):
    box = Outbox(str(tmp_path / "outbox.jsonl"))
    entry = box.enqueue("workflow.submit", {"intent": "run tests"})
    offline = StubTransport("offline")
    first = asyncio.run(reconcile(box, offline))
    assert first["acked"] == [] and len(first["deferred"]) == 1
    assert box.get(entry["id"])["status"] == "pending"
    assert "unreachable" in box.get(entry["id"])["last_error"]
    assert box.get(entry["id"])["attempts"] == 1

    online = StubTransport()
    second = asyncio.run(reconcile(box, online))
    assert len(second["acked"]) == 1 and second["pending_after"] == 0
    assert second["acked"][0]["replayed"] is False


def test_reconcile_ignores_other_entry_kinds(tmp_path):
    box = Outbox(str(tmp_path / "outbox.jsonl"))
    box.enqueue("memory.note", {"content": "not a submission"})
    summary = asyncio.run(reconcile(box, StubTransport()))
    assert summary["checked"] == 0 and summary["pending_after"] == 1
