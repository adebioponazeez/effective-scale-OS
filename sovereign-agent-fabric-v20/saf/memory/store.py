import json

from saf.core.durability import FileLock, fsync_write, read_jsonl, compact_quarantine, json_line


class MemoryStore:
    """Durable JSONL memory with torn-write quarantine.

    Every `remember` is fsync'd before it returns (durable) and serialized by a
    file lock (concurrent writers cannot interleave records). A crash during an
    append can leave a torn tail line; reads quarantine it into
    `<path>.corrupt` instead of raising, so memory never becomes unreadable.
    """

    def __init__(self, path=".saf/memory.jsonl"):
        self.path = path

    def remember(self, kind, content, provenance=None):
        item = {
            "timestamp": self._now(),
            "kind": kind,
            "content": content,
            "provenance": provenance or {},
        }
        with FileLock(self.path):
            fsync_write(self.path, json_line(item))

    def search(self, query):
        q = str(query).lower()
        if not q:
            return []
        records, torn = read_jsonl(self.path)
        if torn:  # torn write detected: quarantine + rewrite atomically
            compact_quarantine(self.path)
        return [r for r in records if q in json.dumps(r, ensure_ascii=False).lower()]

    def all(self):
        records, torn = read_jsonl(self.path)
        if torn:
            compact_quarantine(self.path)
        return records

    def count(self):
        return len(self.all())

    @staticmethod
    def _now():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()
