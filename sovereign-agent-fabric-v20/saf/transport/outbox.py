"""Offline outbox: LOCAL STATE -> CHANGE QUEUE -> OUTBOX -> sync/reconcile.

docs/01 §21 requires weak-network tolerance, resumability, deduplication and
graceful degradation. The outbox is an append-only, fsync'd JSONL log where the
latest record for an id wins (event-sourced; torn tails are tolerated, never
fatal). Every entry carries its id, timestamp, payload hash, dependencies and
status — exactly the fields the architecture names.

`reconcile()` is the "online -> sync -> acknowledge -> reconcile" leg: it
re-submits pending work with the kernel's `Idempotency-Key` set to the payload
hash, so a sync that raced with a successful submit replays instead of
duplicating (the kernel returns `replayed: true`).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from saf.core.durability import FileLock, fsync_write, json_line, read_jsonl


def payload_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(body).hexdigest()


class Outbox:
    def __init__(self, path: str = ".saf/outbox.jsonl"):
        self.path = path

    # ------------------------------------------------------------------ write

    def enqueue(self, kind: str, payload: dict, *, dependencies: list[str] | None = None,
                status: str = "pending") -> dict:
        entry = {
            "id": f"ob-{uuid.uuid4().hex[:12]}",
            "kind": kind,
            "status": status,
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "dependencies": list(dependencies or []),
            "created_at": time.time(),
            "attempts": 0,
            "last_error": None,
        }
        self._append(entry)
        return entry

    def _append(self, record: dict) -> None:
        with FileLock(self.path):
            fsync_write(self.path, json_line(record))

    def ack(self, entry_id: str, result: dict | None = None) -> dict:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        updated = {**entry, "status": "acked", "result": result or {}, "acked_at": time.time()}
        self._append({k: v for k, v in updated.items() if k != "attempts"})
        return updated

    def note_attempt(self, entry_id: str, error: str) -> dict:
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError(entry_id)
        updated = {**entry, "attempts": int(entry.get("attempts", 0)) + 1, "last_error": error}
        self._append(updated)
        return updated

    # ------------------------------------------------------------------- read

    def entries(self) -> list[dict]:
        """Latest state per id, oldest first (deterministic order)."""
        records, _torn = read_jsonl(self.path)
        latest: dict[str, dict] = {}
        order: list[str] = []
        for r in records:
            rid = r.get("id")
            if not rid:
                continue
            if rid not in latest:
                order.append(rid)
            latest[rid] = {**latest.get(rid, {}), **r}
        return [latest[i] for i in order]

    def get(self, entry_id: str) -> dict | None:
        for entry in self.entries():
            if entry["id"] == entry_id:
                return entry
        return None

    def pending(self) -> list[dict]:
        return [e for e in self.entries() if e.get("status") == "pending"]

    def summary(self) -> dict:
        entries = self.entries()
        by_status: dict[str, int] = {}
        for e in entries:
            by_status[e.get("status", "unknown")] = by_status.get(e.get("status", "unknown"), 0) + 1
        return {"total": len(entries), "by_status": by_status,
                "pending": len(self.pending())}


async def reconcile(outbox: Outbox, transport, *, limit: int = 50) -> dict:
    """Sync pending submissions to the kernel; ack the accepted, keep the rest."""
    from saf.core.compiler import compile_intent
    from saf.transport.effective_scale import TransportError

    result = {"checked": 0, "acked": [], "deferred": [], "errors": []}
    for entry in outbox.pending()[:limit]:
        if entry.get("kind") != "workflow.submit":
            continue
        result["checked"] += 1
        payload = entry.get("payload", {})
        task = compile_intent(payload.get("intent", ""))
        try:
            submitted = await transport.submit(
                task, workload_id=payload.get("workload_id"),
                idem_key=entry.get("payload_hash"))
        except TransportError as exc:
            outbox.note_attempt(entry["id"], str(exc))
            result["deferred"].append({"id": entry["id"], "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001 — reconciliation never crashes the caller
            outbox.note_attempt(entry["id"], f"unexpected: {exc}")
            result["errors"].append({"id": entry["id"], "error": str(exc)})
            continue
        outbox.ack(entry["id"], submitted)
        result["acked"].append({"id": entry["id"], "workflow_id": submitted["workflow"]["id"],
                                "replayed": submitted.get("replayed", False)})
    result["pending_after"] = len(outbox.pending())
    return result
