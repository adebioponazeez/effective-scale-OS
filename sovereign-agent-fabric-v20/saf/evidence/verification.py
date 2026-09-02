from saf.evidence.ledger import EvidenceLedger
from saf.tools.filesystem import snapshot


class VerificationEngine:
    """Independent save-proof: inspect actual state, never agent claims."""

    def __init__(self, ledger):
        self.ledger = ledger

    def prove_saved(self, before, target, expected_hash=None):
        """Prove a mutation against the durable target state.

        `before`/`after` are snapshots from `saf.tools.filesystem.snapshot`.
        `verified` requires:
          - target exists after the mutation; and
          - when `expected_hash` is given, the on-disk content matches it
            (the strong, deterministic proof); or
          - when no expected hash is given, the content demonstrably changed.

        `changed` is reported separately: an idempotent save (identical bytes)
        is not a failure, it just can't be proven by delta alone.
        """
        before = before or {}
        after = snapshot(target)
        exists = bool(after.get("exists"))
        after_hash = after.get("hash")
        changed = before.get("hash") != after_hash
        if expected_hash is not None:
            expected_ok = after_hash == expected_hash
            verified = bool(exists and expected_ok)
        else:
            expected_ok = True
            verified = bool(exists and changed)
        result = {
            "changed": changed,
            "expected_hash_ok": expected_ok,
            "before": before,
            "after": after,
            "verified": verified,
        }
        self.ledger.append({"event": "save-proof", **result})
        return result
