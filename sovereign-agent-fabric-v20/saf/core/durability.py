"""Durable, torn-write-safe JSONL primitives for SAF state.

Real-world guarantees this exists for (docs/02-engineering-brief.md, NFR:
Reliability):

* a process crash *during* an append must never corrupt the readable tail
  of the log — pending bytes are quarantined, not fatal;
* concurrent writers (threads, and processes on POSIX) must not interleave
  records or break the evidence hash chain;
* durability is explicit: `fsync` before a write is acknowledged.

Portability note: advisory locking is provided by `fcntl` on POSIX and
`msvcrt` on Windows; on platforms with neither, the lock degrades to an
in-process threading lock so single-process behavior stays correct.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_platform_lock = threading.Lock()


class FileLock:
    """Cross-platform advisory file lock.

    Works across processes on POSIX (fcntl.flock) and Windows (msvcrt.locking).
    The lock file lives next to the target (`<path>.lock`) so the lock survives
    a crash of the holder (the OS releases it) without touching the data file.
    """

    def __init__(self, path: Path, timeout: float = 5.0, poll: float = 0.02):
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")
        self.timeout = timeout
        self.poll = poll
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.lock_path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {self.path} within {self.timeout}s")
                time.sleep(self.poll)

    def _acquire(self) -> None:
        assert self._fh is not None
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc: object) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - Windows
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


def fsync_write(path: Path, data: bytes) -> None:
    """Append `data` and fsync it before returning (durable append)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write `data` atomically: temp file in the same dir + fsync + os.replace.

    Readers see either the complete old file or the complete new file, never a
    mixture — the write is atomic on POSIX and NTFS.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    try:  # best-effort directory fsync so the rename itself is durable
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:  # pragma: no cover - e.g. some filesystems refuse dir fsync
        pass


def json_line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[tuple[int, str]]]:
    """Read JSONL, tolerating torn writes.

    Returns (valid_records, torn_lines) where torn_lines is
    [(1-based_line_no, raw_text), ...]. Torn lines never raise.
    """
    path = Path(path)
    if not path.exists():
        return [], []
    records: list[dict[str, Any]] = []
    torn: list[tuple[int, str]] = []
    try:
        raw = path.read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError:
        return [], [(1, "<unreadable>")]
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            torn.append((line_no, line))
    return records, torn


def quarantine_torn(path: Path, torn: list[tuple[int, str]], suffix: str = ".corrupt") -> int:
    """Move torn lines out of the active log into `<path>.corrupt`.

    The raw bytes are preserved (evidence, never silently erased) but the
    active log no longer contains unparseable data. Returns number moved.
    """
    if not torn:
        return 0
    path = Path(path)
    target = Path(f"{path}{suffix}")
    out = []
    for line_no, text in torn:
        out.append(f"# torn line {line_no} (quarantined {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})\n")
        out.append(text + "\n")
    fsync_write(target, "".join(out).encode("utf-8", errors="surrogateescape"))
    return len(torn)


def compact_quarantine(path: Path) -> tuple[int, int]:
    """Quarantine torn lines and rewrite the active log without them.

    Returns (kept_records, quarantined_lines). The rewrite is atomic and
    serialized against concurrent writers.
    """
    with FileLock(path):
        records, torn = read_jsonl(path)
        moved = quarantine_torn(path, torn)
        if records and not torn:
            return len(records), 0
        if moved or torn:
            atomic_write(path, b"".join(json_line(r) for r in records))
        return len(records), moved
