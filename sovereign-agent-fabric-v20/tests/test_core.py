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
