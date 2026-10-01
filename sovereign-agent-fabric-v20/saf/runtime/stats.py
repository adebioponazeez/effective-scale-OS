"""Observed resource reliability (the "ranking update" step, made durable).

The resolver stays deterministic (fit/trust/cost/environment, docs §9): ranking
is a pure function of registry state. What execution adds is *observability* of
what actually worked. This store keeps a durable per-resource counter and is
read back by `saf resources --stats` — evidence for operators, not a hidden
ranking mutation.
"""
from __future__ import annotations

from saf.core.durability import FileLock, fsync_write, json_line, read_jsonl


class ResourceStats:
    def __init__(self, path: str = ".saf/resource-stats.jsonl"):
        self.path = path

    def record(self, resource_id: str, ok: bool) -> None:
        current = self.latest().get(resource_id, {"attempts": 0, "successes": 0, "failures": 0})
        current = {**current, "resource_id": resource_id,
                   "attempts": int(current.get("attempts", 0)) + 1}
        if ok:
            current["successes"] = int(current.get("successes", 0)) + 1
        else:
            current["failures"] = int(current.get("failures", 0)) + 1
        with FileLock(self.path):
            fsync_write(self.path, json_line(current))

    def latest(self) -> dict[str, dict]:
        records, _torn = read_jsonl(self.path)
        out: dict[str, dict] = {}
        for r in records:
            rid = r.get("resource_id")
            if rid:
                out[rid] = r
        return out

    def summary(self) -> list[dict]:
        out = []
        for rid, row in sorted(self.latest().items()):
            attempts = int(row.get("attempts", 0)) or 1
            out.append({
                "resource_id": rid,
                "attempts": int(row.get("attempts", 0)),
                "successes": int(row.get("successes", 0)),
                "failures": int(row.get("failures", 0)),
                "success_rate": round(int(row.get("successes", 0)) / attempts, 4),
            })
        return out
