from saf.core.compiler import compile_intent
from saf.core.registry import Registry
from saf.core.resolver import CapabilityResolver
from saf.core.contracts import ExecutionContext
from saf.agents.cli import PiAdapter,CursorCliAdapter

def test_compile():
    t=compile_intent("refactor repository and run tests")
    assert "cap://software/code/refactor" in t.required_capabilities
    assert "cap://software/testing/execute" in t.required_capabilities

def test_resolver():
    r=Registry(); r.register("agent://pi",PiAdapter()); r.register("agent://cursor-cli",CursorCliAdapter())
    ranked=CapabilityResolver(r).resolve(compile_intent("refactor code"),ExecutionContext(task_id="t"))
    assert ranked and ranked[0].resource_id=="agent://cursor-cli"
