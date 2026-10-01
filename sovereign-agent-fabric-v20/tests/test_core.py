from saf.core.compiler import compile_intent
from saf.core.registry import Registry
from saf.core.resolver import CapabilityResolver
from saf.core.contracts import ExecutionContext
from saf.agents.cli import PiAdapter,CursorCliAdapter
import unittest

class TestCompilerKeywords(unittest.TestCase):
    """Keyword matching must not fire on unrelated words (found via the web runtime tests)."""

    def test_report_does_not_imply_repository_inspection(self):
        from saf.core.compiler import compile_intent

        task = compile_intent("write a report about the weekly numbers")
        self.assertNotIn("cap://software/repository/inspect", task.required_capabilities)

    def test_repository_spellings_still_match(self):
        from saf.core.compiler import compile_intent

        for intent in ("inspect the repository", "inspect repos", "inspect the repositories"):
            task = compile_intent(intent)
            self.assertIn("cap://software/repository/inspect", task.required_capabilities, intent)

    def test_research_intent_carries_bounded_urls(self):
        from saf.core.compiler import MAX_URLS_PER_INTENT, compile_intent

        task = compile_intent("research https://example.com/a and https://example.com/b")
        self.assertEqual(task.required_capabilities, ["cap://research/web"])
        self.assertEqual(task.constraints["urls"],
                         ["https://example.com/a", "https://example.com/b"])

        many = compile_intent("research " + " ".join(f"https://example.com/{i}" for i in range(30)))
        self.assertEqual(len(many.constraints["urls"]), MAX_URLS_PER_INTENT)

    def test_research_without_urls_has_no_constraint(self):
        from saf.core.compiler import compile_intent

        task = compile_intent("research the web")
        self.assertEqual(task.required_capabilities, ["cap://research/web"])
        self.assertNotIn("urls", task.constraints)


def test_compile():
    t=compile_intent("refactor repository and run tests")
    assert "cap://software/code/refactor" in t.required_capabilities
    assert "cap://software/testing/execute" in t.required_capabilities

def test_resolver():
    r=Registry(); r.register("agent://pi",PiAdapter()); r.register("agent://cursor-cli",CursorCliAdapter())
    ranked=CapabilityResolver(r).resolve(compile_intent("refactor code"),ExecutionContext(task_id="t"))
    assert ranked and ranked[0].resource_id=="agent://cursor-cli"

def test_id_helpers_are_unique_and_labeled():
    """Worker ids show up in kernel attempt rows — they must be readable and unique."""
    from saf.core.ids import new_execution_id, new_worker_id

    a, b = new_execution_id(), new_execution_id()
    assert a.startswith("exec-") and len(a) == len("exec-") + 16 and a != b
    w1 = new_worker_id("saf")
    w2 = new_worker_id("saf")
    assert w1.startswith("saf-") and w1 != w2
    assert new_worker_id("cli").startswith("cli-")
    assert " " not in w1
