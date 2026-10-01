"""Rollback points: snapshot -> mutate -> restore (docs §30 step 8)."""
from pathlib import Path

from saf.tools.backup import BackupStore


def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "a.py").write_text("a=1\n")
    (ws / "keep.txt").write_text("keep\n")
    return ws


def test_restore_modified_file(tmp_path):
    ws = make_ws(tmp_path)
    store = BackupStore(str(tmp_path / "backups"))
    manifest = store.create_point("exec-1", ws)
    assert manifest["stored"] == 2 and manifest["too_large"] == 0

    (ws / "pkg" / "a.py").write_text("a=999  # mutated\n")
    plan = store.plan("exec-1")
    assert {p["path"]: p["action"] for p in plan} == {
        "keep.txt": "unchanged", "pkg/a.py": "restore_content"}

    result = store.restore("exec-1")
    assert result["ok"] and result["restored"] == ["pkg/a.py"]
    assert (ws / "pkg" / "a.py").read_text() == "a=1\n"


def test_restore_deletes_files_created_after_the_point(tmp_path):
    ws = make_ws(tmp_path)
    store = BackupStore(str(tmp_path / "backups"))
    store.create_point("exec-2", ws, paths=["new.txt"])
    (ws / "new.txt").write_text("created by the execution\n")
    result = store.restore("exec-2")
    assert result["deleted"] == ["new.txt"] and not (ws / "new.txt").exists()


def test_large_files_are_recorded_but_reported_unrestorable(tmp_path):
    ws = make_ws(tmp_path)
    big = ws / "big.bin"
    big.write_bytes(b"x" * 4096)
    store = BackupStore(str(tmp_path / "backups"), max_bytes=1024)
    manifest = store.create_point("exec-3", ws)
    assert manifest["too_large"] == 1

    big.write_bytes(b"y" * 4096)
    result = store.restore("exec-3")
    assert result["ok"] is False
    assert result["unrestorable"] == [{"path": "big.bin", "reason": "exceeds max_bytes"}]
    assert big.read_bytes() == b"y" * 4096  # untouched: never silently destroyed


def test_dry_run_never_touches_disk(tmp_path):
    ws = make_ws(tmp_path)
    store = BackupStore(str(tmp_path / "backups"))
    store.create_point("exec-4", ws)
    (ws / "keep.txt").write_text("changed\n")
    result = store.restore("exec-4", dry_run=True)
    assert result["dry_run"] and result["restored"] == ["keep.txt"]
    assert (ws / "keep.txt").read_text() == "changed\n"


def test_missing_point_is_a_structured_error(tmp_path):
    store = BackupStore(str(tmp_path / "backups"))
    assert store.restore("nope") == {"ok": False, "error": "no rollback point for nope"}
    assert store.plan("nope") == []


def test_create_point_is_idempotent_and_indexed(tmp_path):
    ws = make_ws(tmp_path)
    store = BackupStore(str(tmp_path / "backups"))
    first = store.create_point("exec-5", ws)
    (ws / "keep.txt").write_text("later\n")
    second = store.create_point("exec-5", ws)
    assert first["created_at"] == second["created_at"]
    assert [p["execution_id"] for p in store.points()] == ["exec-5"]
    restored = store.restore("exec-5")
    assert restored["restored"] == ["keep.txt"]          # mutated after the point
    assert restored["unchanged"] == ["pkg/a.py"]         # never touched
    assert (ws / "keep.txt").read_text() == "keep\n"    # point, not the later value


def test_restore_targets_a_chosen_workspace(tmp_path):
    """Restore replays the recorded state; it never invents untracked files."""
    ws = make_ws(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("unrelated\n")
    store = BackupStore(str(tmp_path / "backups"))
    store.create_point("exec-6", ws, paths=["keep.txt"])
    (ws / "keep.txt").write_text("changed\n")
    result = store.restore("exec-6", workspace=other)
    assert result["restored"] == ["keep.txt"]
    assert (other / "keep.txt").read_text() == "keep\n"
    assert [p.name for p in other.iterdir()] == ["keep.txt"]  # tracked set is respected
