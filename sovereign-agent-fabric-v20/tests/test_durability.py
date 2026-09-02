"""Chaos/storm proofs for durable state: torn writes, tamper, concurrency.

These exercise the failure modes in docs/02-engineering-brief.md:
process crash mid-write, duplicate/concurrent writers, silent rewrites.
"""
import threading

import pytest

from saf.memory.store import MemoryStore
from saf.evidence.ledger import EvidenceLedger
from saf.evidence.verification import VerificationEngine
from saf.core.durability import atomic_write, read_jsonl


# ---------------------------------------------------------------- MemoryStore


def test_remember_is_durable_and_readable(tmp_path):
    m = MemoryStore(tmp_path / "memory.jsonl")
    m.remember("episodic", "refactored auth", {"agent": "agent://pi"})
    m.remember("semantic", "tests green", {"verified": True})
    assert m.count() == 2
    assert len(m.search("auth")) == 1
    assert len(m.search("GREEN")) == 1  # case-insensitive
    assert m.search("missing") == []


def test_torn_write_is_quarantined_not_fatal(tmp_path):
    """Crash during append: torn tail must never break subsequent reads."""
    p = tmp_path / "memory.jsonl"
    m = MemoryStore(p)
    m.remember("episodic", "before crash", {})
    # simulate a torn last line (partial JSON from a killed writer)
    p.write_bytes(p.read_bytes() + b'{"timestamp":"2025-01-01T00:00:00","kind":"part')

    results = m.search("before crash")
    assert len(results) == 1  # valid records still returned
    assert p.exists()
    # the torn bytes were moved to the quarantine file, active log is whole
    assert p.with_name("memory.jsonl.corrupt").exists()
    records, torn = read_jsonl(p)
    assert torn == []
    assert len(records) == 1
    # new appends continue to work after quarantine
    m.remember("episodic", "after repair", {})
    assert m.count() == 2


def test_concurrent_remember_records_intact(tmp_path):
    """Storm: 8 threads x 50 writes; every record must parse, none lost."""
    p = tmp_path / "memory.jsonl"
    m = MemoryStore(p)

    def writer(n):
        for i in range(50):
            m.remember("episodic", f"thread-{n}-item-{i}", {"thread": n})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records, torn = read_jsonl(p)
    assert torn == []
    assert len(records) == 400
    contents = {r["content"] for r in records}
    assert len(contents) == 400  # no duplicate/lost records


# ----------------------------------------------------------------------- Ledger


def test_ledger_chain_verifies(tmp_path):
    l = EvidenceLedger(tmp_path / "evidence.jsonl")
    r1 = l.append({"event": "save-proof", "ok": True})
    r2 = l.append({"event": "mutation", "target": "x.py"})
    r3 = l.append({"event": "save-proof", "ok": True})
    assert r1["previous_hash"] == ""
    assert r2["previous_hash"] == r1["hash"]
    assert r3["previous_hash"] == r2["hash"]
    v = l.verify()
    assert v["ok"] is True
    assert v["records"] == 3
    assert v["torn"] == 0
    assert v["issues"] == []


def test_ledger_detects_tampering(tmp_path):
    """Silent rewrite of a middle record must be caught by the chain."""
    p = tmp_path / "evidence.jsonl"
    l = EvidenceLedger(p)
    l.append({"event": "a", "payload": "original"})
    l.append({"event": "b", "payload": "original"})
    l.append({"event": "c", "payload": "original"})
    v = l.verify()
    assert v["ok"] is True

    # tamper: change content of the middle record in place
    raw = p.read_text().splitlines()
    middle = __import__("json").loads(raw[1])
    middle["payload"] = "forged"
    raw[1] = __import__("json").dumps(middle, sort_keys=True)
    p.write_bytes(("\n".join(raw) + "\n").encode())

    v2 = l.verify()
    assert v2["ok"] is False
    assert any("hash mismatch" in i for i in v2["issues"])


def test_ledger_torn_tail_reported_and_quarantined(tmp_path):
    p = tmp_path / "evidence.jsonl"
    l = EvidenceLedger(p)
    l.append({"event": "one"})
    l.append({"event": "two"})
    p.write_bytes(p.read_bytes() + b'{"event":"thr')  # torn third record

    v = l.verify()
    assert v["ok"] is False
    assert v["torn"] == 1

    moved = l.quarantine()
    assert moved == 1
    v2 = l.verify()
    assert v2["ok"] is True
    assert v2["records"] == 2
    # chain still intact after quarantine: appends continue from last valid
    l.append({"event": "three"})
    assert l.verify()["ok"] is True


def test_ledger_concurrent_appends_chain_intact(tmp_path):
    """Storm: 8 threads x 25 appends; chain must verify with zero gaps."""
    p = tmp_path / "evidence.jsonl"
    l = EvidenceLedger(p)

    def writer(n):
        for i in range(25):
            l.append({"event": "storm", "thread": n, "i": i})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    v = l.verify()
    assert v["ok"] is True, v
    assert v["records"] == 200
    assert v["torn"] == 0


# ------------------------------------------------------ verification + atomic


def test_prove_saved_with_expected_hash(tmp_path):
    target = tmp_path / "result.json"
    target.write_text('{"ok": true}')
    before = {"exists": False, "hash": None}
    eng = VerificationEngine(EvidenceLedger(tmp_path / "ledger.jsonl"))

    result = eng.prove_saved(before, target, expected_hash=None)
    assert result["changed"] is True
    assert result["verified"] is True

    # exact expected hash: the strong idempotent proof
    import hashlib

    expected = hashlib.sha256(target.read_bytes()).hexdigest()
    result2 = eng.prove_saved(before, target, expected_hash=expected)
    assert result2["verified"] is True
    assert eng.ledger.verify()["ok"] is True


def test_prove_saved_rejects_missing_or_wrong_content(tmp_path):
    eng = VerificationEngine(EvidenceLedger(tmp_path / "ledger.jsonl"))
    missing = eng.prove_saved({}, tmp_path / "never-written.txt")
    assert missing["verified"] is False

    target = tmp_path / "r.txt"
    target.write_text("stable")
    wrong = eng.prove_saved({}, target, expected_hash="deadbeef")
    assert wrong["verified"] is False
    assert wrong["expected_hash_ok"] is False


def test_atomic_write_never_exposes_partial_content(tmp_path):
    p = tmp_path / "state.json"
    atomic_write(p, b'{"v":1}')
    for _ in range(50):
        atomic_write(p, b'{"v":2,"pad":"' + b"x" * 4096 + b'"}')
    # a partial temp file must never appear in the active path
    assert not list(tmp_path.glob("state.json.tmp-*"))
    assert p.read_text().startswith('{"v":2')
