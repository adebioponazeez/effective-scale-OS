"""Hardened behavior proofs: boundaries, timeouts, provider failure mapping."""
import asyncio
import sys

import pytest

from saf.core.compiler import compile_intent
from saf.core.contracts import ExecutionContext, Task, Autonomy, AgentRequest
from saf.core.policy import PolicyEngine
from saf.models.openrouter import OpenRouterAdapter
from saf.agents.cli import CliAgentRuntime


# ------------------------------------------------------------------ compiler


def test_compiler_word_boundaries():
    # "test" must not match "latest"/"contest"; "git" must not match "digit".
    t = compile_intent("fetch the latest release notes")
    assert "cap://software/testing/execute" not in t.required_capabilities
    t2 = compile_intent("run the tests")
    assert "cap://software/testing/execute" in t2.required_capabilities
    t3 = compile_intent("digitize records")
    assert "cap://software/git/operate" not in t3.required_capabilities


def test_compiler_dedupes_and_falls_back():
    t = compile_intent("test the tests")
    assert t.required_capabilities == ["cap://software/testing/execute"]
    empty = compile_intent("just think")
    assert empty.required_capabilities == ["cap://general/agent/execute"]


def test_compiler_doc_pipeline_keywords():
    # docs/01 §10 pipeline: commit + verify are canonical graph nodes.
    t = compile_intent("commit and verify the result")
    assert "cap://software/git/operate" in t.required_capabilities
    assert "cap://verification/save-proof" in t.required_capabilities


# --------------------------------------------------------------------- policy


def test_policy_blocks_destructive_execution():
    p = PolicyEngine()
    task = Task(intent="delete the staging database", autonomy=Autonomy.A3)
    ok, reason = p.authorize(task)
    assert ok is False
    assert "approval" in reason


def test_policy_word_boundaries():
    p = PolicyEngine()
    # "undelete" is not "delete" — no false block; "deploy" in "redeployed"
    # is not the destructive verb either (verbatim policy set).
    assertive = Task(intent="undelete the file", autonomy=Autonomy.A3)
    assert p.authorize(assertive)[0] is True
    # planning autonomy (A2) cannot execute side effects: no block needed yet
    plan = Task(intent="delete everything", autonomy=Autonomy.A2)
    assert p.authorize(plan)[0] is True


# ----------------------------------------------------------------- CLI runtime


class _EchoRuntime(CliAgentRuntime):
    binary = sys.executable
    argv = ["-c", "import sys; print('argv=' + repr(sys.argv[1:])); sys.exit(0)"]


class _SleepRuntime(CliAgentRuntime):
    binary = sys.executable
    argv = ["-c", "import time; time.sleep(30)"]
    timeout_s = 0.2


def _request(workspace="."):
    return AgentRequest(
        task=Task(intent="x"),
        context=ExecutionContext(task_id="t", workspace=str(workspace)),
        prompt="hello",
    )


def test_cli_runtime_missing_binary():
    r = CliAgentRuntime()
    r.binary = "definitely-not-a-real-binary-xyz"
    res = asyncio.run(r.execute(_request()))
    assert res.ok is False
    assert "not installed" in res.summary


def test_cli_runtime_missing_workspace(tmp_path):
    r = _EchoRuntime()
    res = asyncio.run(r.execute(_request(tmp_path / "nope")))
    assert res.ok is False
    assert "workspace does not exist" in res.summary


def test_cli_runtime_args_are_not_flag_injectable():
    """Prompt is passed as ONE final argument; flags in the prompt stay data."""
    r = _EchoRuntime()
    res = asyncio.run(r.execute(_request()))
    assert res.ok is True
    lines = [l for l in res.stdout.splitlines() if l.startswith("argv=")]
    assert lines == ["argv=['hello']"]  # single arg, no shell/flag interpretation


def test_cli_runtime_timeout_is_bounded():
    r = _SleepRuntime()
    res = asyncio.run(r.execute(_request()))
    assert res.ok is False
    assert "timed out" in res.summary


# ------------------------------------------------------------- model provider


def test_openrouter_missing_key_is_structured(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    res = asyncio.run(OpenRouterAdapter().generate(
        __import__("saf.core.contracts", fromlist=["ModelRequest"]).ModelRequest(
            prompt="hi", system="you are safe")))
    assert res.ok is False
    assert "OPENROUTER_API_KEY" in res.text


def test_openrouter_http_error_maps_to_result(monkeypatch):
    import urllib.error

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

        def read(self):
            return b'{"error":"rate limited"}'

    def fake_urlopen(*a, **k):
        raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, _Resp())

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    res = asyncio.run(OpenRouterAdapter().generate(
        __import__("saf.core.contracts", fromlist=["ModelRequest"]).ModelRequest(prompt="hi")))
    assert res.ok is False
    assert "HTTP 429" in res.text


def test_resolver_offline_penalizes_remote_models(tmp_path):
    from saf.core.registry import Registry
    from saf.core.resolver import CapabilityResolver
    from saf.models.openrouter import OpenRouterAdapter
    from saf.agents.cli import PiAdapter

    r = Registry()
    r.register("model-provider://openrouter", OpenRouterAdapter())
    r.register("agent://pi", PiAdapter())
    task = compile_intent("refactor code")
    online = CapabilityResolver(r).resolve(task, ExecutionContext(task_id="t", network_available=True))
    offline = CapabilityResolver(r).resolve(task, ExecutionContext(task_id="t", network_available=False))
    online_router_score = next(c.score for c in online if c.resource_id == "model-provider://openrouter")
    offline_router_score = next(c.score for c in offline if c.resource_id == "model-provider://openrouter")
    assert offline_router_score < online_router_score
