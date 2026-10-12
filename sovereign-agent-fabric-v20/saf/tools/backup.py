"""Rollback points: snapshot -> execute -> restore (docs/01 §30 step 8).

A rollback point is a durable, content-addressed record of a workspace taken
*before* a mutating capability runs. Restore puts every tracked path back to its
recorded state: content is written back (and hash-verified), and files that did
not exist at the point are removed.

Honest boundaries:
  * content is stored only up to `max_bytes` per file — bigger files are
    hash-recorded but reported as `unrestorable_too_large` on restore;
  * the walk is bounded by `max_files` (deterministic sorted order);
  * files outside the tracked set are never touched, so a restore cannot
    silently delete work the rollback point never knew about.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from saf.core.durability import FileLock, atomic_write, json_line, read_jsonl
from saf.tools.filesystem import sha256_file

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".saf", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target"}


class BackupStore:
    """Content-addressed workspace backups + manifests, torn-write safe."""

    def __init__(self, root: str | Path = ".saf/backups", *, max_files: int = 2000,
                 max_bytes: int = 8 * 1024 * 1024):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.manifests = self.root / "manifests"
        self.index_path = self.root / "index.jsonl"
        self.max_files = int(max_files)
        self.max_bytes = int(max_bytes)

    # ------------------------------------------------------------------ write

    def create_point(self, execution_id: str, workspace: str | Path = ".",
                     paths: list[str] | None = None) -> dict:
        """Record the current state of `paths` (or the whole workspace) durably."""
        workspace = Path(workspace).resolve()
        manifest_path = self.manifest_path(execution_id)
        if manifest_path.exists():
            existing = self.manifest(execution_id)
            if existing is not None:
                return existing
        tracked = self._track(workspace, paths)
        entries = [self._record(workspace, rel) for rel in tracked]
        created_at = _now()
        manifest = {
            "execution_id": execution_id,
            "workspace": str(workspace),
            "created_at": created_at,
            "max_bytes": self.max_bytes,
            "entries": entries,
            "stored": sum(1 for e in entries if e["object"]),
            "too_large": sum(1 for e in entries if e["existed"] and not e["object"]),
        }
        with FileLock(self.index_path):
            atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode())
            from saf.core.durability import fsync_write

            fsync_write(self.index_path, json_line({
                "execution_id": execution_id, "workspace": str(workspace),
                "created_at": created_at, "files": len(entries),
            }))
        return manifest

    def _record(self, workspace: Path, rel: str) -> dict:
        path = workspace / rel
        entry = {"path": rel, "existed": False, "sha256": None, "size": 0, "object": None}
        if not path.is_file():
            return entry
        digest = sha256_file(path)
        size = path.stat().st_size
        entry.update({"existed": True, "sha256": digest, "size": size})
        if size <= self.max_bytes:
            target = self.object_path(digest)
            if not target.exists():
                data = path.read_bytes()
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, data)
            entry["object"] = digest
        return entry

    def _track(self, workspace: Path, paths: list[str] | None) -> list[str]:
        if paths:
            out = []
            for raw in paths:
                p = Path(raw)
                rel = str(p if not p.is_absolute() else p.relative_to(workspace))
                if rel not in out:
                    out.append(rel)
            return sorted(out)
        tracked: list[str] = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                if len(tracked) >= self.max_files:
                    return tracked
                tracked.append(os.path.relpath(os.path.join(root, name), workspace))
        return tracked

    # ------------------------------------------------------------------- read

    def manifest_path(self, execution_id: str) -> Path:
        return self.manifests / f"{execution_id}.json"

    def manifest(self, execution_id: str) -> dict | None:
        path = self.manifest_path(execution_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def points(self) -> list[dict]:
        records, _torn = read_jsonl(self.index_path)
        seen: dict[str, dict] = {}
        for r in records:
            seen[r.get("execution_id", "")] = r
        return [seen[k] for k in sorted(seen) if k]

    def object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def plan(self, execution_id: str, *, workspace: str | Path | None = None) -> list[dict]:
        """What a restore would do — read-only, safe to call any time."""
        manifest = self.manifest(execution_id)
        if manifest is None:
            return []
        base = Path(workspace).resolve() if workspace else Path(manifest["workspace"])
        out = []
        for entry in manifest["entries"]:
            current = base / entry["path"]
            exists = current.is_file()
            digest = sha256_file(current) if exists else None
            if entry["existed"] and not entry["object"]:
                action = "unrestorable_too_large"
            elif entry["existed"] and digest != entry["sha256"]:
                action = "restore_content"
            elif entry["existed"] and digest == entry["sha256"]:
                action = "unchanged"
            elif not entry["existed"] and exists:
                action = "delete_created"
            else:
                action = "unchanged"
            out.append({"path": entry["path"], "action": action,
                        "recorded_sha256": entry["sha256"], "current_sha256": digest})
        return out

    # ---------------------------------------------------------------- restore

    def restore(self, execution_id: str, *, workspace: str | Path | None = None,
                dry_run: bool = False) -> dict:
        manifest = self.manifest(execution_id)
        if manifest is None:
            return {"ok": False, "error": f"no rollback point for {execution_id}"}
        plan = self.plan(execution_id, workspace=workspace)
        base = Path(workspace).resolve() if workspace else Path(manifest["workspace"])
        result = {"execution_id": execution_id, "workspace": str(base), "dry_run": dry_run,
                  "restored": [], "deleted": [], "unchanged": [], "unrestorable": []}
        by_path = {e["path"]: e for e in manifest["entries"]}
        for item in plan:
            action, rel = item["action"], item["path"]
            if action == "unchanged":
                result["unchanged"].append(rel)
                continue
            if action == "unrestorable_too_large":
                result["unrestorable"].append({"path": rel, "reason": "exceeds max_bytes"})
                continue
            if dry_run:
                (result["restored"] if action == "restore_content" else result["deleted"]).append(rel)
                continue
            target = base / rel
            if action == "restore_content":
                entry = by_path[rel]
                data = self.object_path(entry["object"]).read_bytes()
                if sha256_bytes(data) != entry["sha256"]:
                    result["unrestorable"].append({"path": rel, "reason": "object store corrupted"})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, data)
                result["restored"].append(rel)
            elif action == "delete_created":
                try:
                    target.unlink()
                    result["deleted"].append(rel)
                except OSError as exc:
                    result["unrestorable"].append({"path": rel, "reason": str(exc)})
        result["ok"] = not result["unrestorable"]
        return result


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
