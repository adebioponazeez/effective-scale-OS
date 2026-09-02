import hashlib
import json

from saf.core.durability import FileLock, fsync_write, read_jsonl, atomic_write, json_line


def _hash_payload(record: dict) -> bytes:
    """Deterministic hash input: all fields except the hash itself."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


class EvidenceLedger:
    """Append-only, hash-chained evidence ledger with tamper detection.

    - Each record commits to the previous record's hash (tamper-evident chain).
    - Appends are serialized by a file lock and fsync'd before return.
    - Reads tolerate torn writes: broken tail lines are reported (and can be
      quarantined) instead of crashing `verify` or the next `append`.
    """

    def __init__(self, path=".saf/evidence.jsonl"):
        self.path = path

    def append(self, event):
        record = {"timestamp": self._now(), "previous_hash": "", **event}
        # Lock spans read-last + hash + write so concurrent appenders chain correctly.
        with FileLock(self.path):
            records, _torn = read_jsonl(self.path)
            previous = ""
            if records:
                previous = records[-1].get("hash", "")
            record["previous_hash"] = previous
            record["hash"] = hashlib.sha256(_hash_payload(record)).hexdigest()
            fsync_write(self.path, json_line(record))
        return record

    def verify(self):
        """Recompute the chain; detect tampering, gaps, and torn writes.

        Returns {"ok", "records", "torn", "issues"}; `ok` is True only when the
        chain is intact and there are no torn lines.
        """
        records, torn = read_jsonl(self.path)
        issues = []
        prev = ""
        for i, rec in enumerate(records):
            if rec.get("previous_hash") != prev:
                issues.append(f"record[{i}]: previous_hash mismatch")
            if rec.get("hash") != hashlib.sha256(_hash_payload(rec)).hexdigest():
                issues.append(f"record[{i}]: hash mismatch (tampered or rewritten)")
            prev = rec.get("hash", "")
        return {
            "ok": not issues and not torn,
            "records": len(records),
            "torn": len(torn),
            "issues": issues,
        }

    def quarantine(self):
        """Move torn lines to `<path>.corrupt` and rewrite the log atomically.

        Only unparseable lines are removed; valid records and the chain are
        preserved. Returns number of quarantined lines.
        """
        from saf.core.durability import FileLock, quarantine_torn

        with FileLock(self.path):
            records, torn = read_jsonl(self.path)
            if not torn:
                return 0
            moved = quarantine_torn(self.path, torn)
            atomic_write(self.path, b"".join(json_line(r) for r in records))
        return moved

    def all(self):
        records, _torn = read_jsonl(self.path)
        return records

    @staticmethod
    def _now():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()
