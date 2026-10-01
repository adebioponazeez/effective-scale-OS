"""`cap://software/git/operate` — the deterministic git runtime (saf://git).

The capability was declared and compiler-emitted but had no implementation, so every git
intent ended as "unavailable" (tracked in ontology/known-gaps.json). These tests pin the
contract that made it safe to implement without handing an agent a shell:

  * fixed subcommand vocabulary, `create_subprocess_exec` only (no shell);
  * the commit message is one argv element and bounded, so shell metacharacters are text;
  * the only mutation happens when a message is supplied — otherwise it is read-only;
  * a missing repository, a clean tree and an invalid path all produce honest answers.
"""
import asyncio
import subprocess
import sys

import pytest

from saf.agents.git import HARD_MAX_MESSAGE_CHARS, GitRuntime
from saf.core.contracts import AgentRequest, ExecutionContext, Task
from saf.core.policy import PolicyEngine

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                                reason="git is not installed")


def _request(workspace, **constraints):
    return AgentRequest(
        task=Task(intent="git commit", required_capabilities=["cap://software/git/operate"],
                  constraints=constraints),
        context=ExecutionContext(task_id="test", platform=sys.platform, workspace=str(workspace)),
        prompt="git commit the changes",
    )


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "saf@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "SAF Test"], cwd=workspace, check=True)
    (workspace / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workspace, check=True)
    return workspace


def _run(request):
    return asyncio.run(GitRuntime().execute(request))


def test_registered_in_the_default_registry():
    from saf.runtime.bootstrap import build_registry

    registry = build_registry()
    assert "saf://git" in {r.resource_id for r in registry.all()}
    resolver = registry.all()
    git = next(r for r in resolver if r.resource_id == "saf://git")
    assert "cap://software/git/operate" in git.capability_ids


def test_non_repository_is_reported_not_crashed(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _run(_request(plain, message="commit something"))
    assert result.ok is False
    assert "not a git repository" in result.summary


def test_status_is_read_only_and_does_not_commit(repo):
    (repo / "app.py").write_text("VALUE = 2\n")
    before = _git(repo, "rev-list", "--count", "HEAD").strip()
    result = _run(_request(repo))  # no message -> status only
    assert result.ok is True
    assert "1 change" in result.summary
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == before
    evidence = result.evidence[0]
    assert evidence["kind"] == "git-operate" and evidence["operation"] == "status"
    assert evidence["changes"] == 1


def test_clean_tree_commit_is_an_honest_no_op(repo):
    result = _run(_request(repo, message="nothing to do"))
    assert result.ok is True
    assert "nothing to commit" in result.summary
    assert result.evidence[0]["committed"] is False
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_commit_records_evidence_and_verifies(repo):
    (repo / "app.py").write_text("VALUE = 3\n")
    (repo / "new.txt").write_text("hello\n")
    result = _run(_request(repo, message="feat: bump value"))
    assert result.ok is True
    assert "committed 2 file(s)" in result.summary
    assert sorted(result.artifacts) == ["app.py", "new.txt"]
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    evidence = result.evidence[0]
    assert evidence["committed"] is True and evidence["sha"] and len(evidence["files"]) == 2
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                          capture_output=True, text=True).stdout.strip() == ""


def test_message_is_data_never_shell_syntax(repo):
    """A hostile message must be stored literally: no shell, no interpolation."""
    hostile = 'x"; rm -rf .; echo $(whoami) `id` && true'
    (repo / "app.py").write_text("VALUE = 4\n")
    result = _run(_request(repo, message=hostile))
    assert result.ok is True
    committed = _git(repo, "log", "-1", "--pretty=%B").strip()
    assert committed == hostile[:200]
    assert (repo / "app.py").exists()  # nothing was deleted


def test_message_length_is_bounded(repo):
    """Default cap is conservative; an explicit larger cap is clamped to the hard limit."""
    (repo / "app.py").write_text("VALUE = 5\n")
    result = _run(_request(repo, message="m" * 5000))
    assert result.ok is True
    assert result.evidence[0]["message_chars"] == 200
    assert len(_git(repo, "log", "-1", "--pretty=%B").strip()) == 200

    (repo / "app.py").write_text("VALUE = 5.1\n")
    result = _run(_request(repo, message="m" * 5000, max_message_chars=5000))
    assert result.evidence[0]["message_chars"] == HARD_MAX_MESSAGE_CHARS
    assert len(_git(repo, "log", "-1", "--pretty=%B").strip()) == HARD_MAX_MESSAGE_CHARS


def test_state_directory_is_never_committed(repo):
    """The fabric's own bookkeeping (.saf: evidence, backups, outbox) must not enter the repo."""
    state = repo / ".saf"
    (state / "backups").mkdir(parents=True)
    (state / "evidence.jsonl").write_text('{"seq": 1}\n')
    (state / "backups" / "blob").write_text("internal\n")
    (repo / "app.py").write_text("VALUE = 9\n")

    result = _run(_request(repo, message="feat: real change only"))
    assert result.ok is True
    tracked = _git(repo, "ls-files").split()
    assert "app.py" in tracked
    assert not [path for path in tracked if path.startswith(".saf")], tracked

def test_paths_must_stay_inside_the_repository(repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    result = _run(_request(repo, message="escape", paths=["../outside.txt"]))
    assert result.ok is False
    assert "workspace-relative" in result.summary
    assert outside.exists()


def test_explicit_status_operation_wins_over_a_message(repo):
    (repo / "app.py").write_text("VALUE = 6\n")
    result = _run(_request(repo, operation="status", message="do not commit me"))
    assert result.ok is True
    assert result.evidence[0]["operation"] == "status"
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"


def test_commit_requires_a_message(repo):
    (repo / "app.py").write_text("VALUE = 7\n")
    result = _run(_request(repo, operation="commit"))
    assert result.ok is False
    assert "non-empty constraints.message" in result.summary


def test_policy_engine_allows_the_git_capability():
    from saf.core.compiler import compile_intent

    task = compile_intent("git commit the changes")
    assert task.required_capabilities == ["cap://software/git/operate"]
    allowed, reason = PolicyEngine().authorize(task)
    assert allowed, reason


def test_executor_end_to_end_commits_through_a_plan(repo, tmp_path):
    """The capability is reachable the way the product uses it: compile -> execute -> ledger."""
    from saf.runtime.bootstrap import build_executor

    (repo / "app.py").write_text("VALUE = 8\n")
    state = tmp_path / ".saf"
    executor = build_executor(str(repo), state_dir=str(state))
    request = _request(repo, message="chore: executed by the fabric")
    plan_result = asyncio.run(executor.execute(compile_with_message(request)))
    assert plan_result.ok is True
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "2"
    assert (state / "evidence.jsonl").exists()


def compile_with_message(request):
    """The executor compiles from the intent; carry the message through constraints."""
    from saf.core.compiler import compile_intent

    task = compile_intent("git commit the changes")
    task.constraints.update(request.task.constraints)
    return task
