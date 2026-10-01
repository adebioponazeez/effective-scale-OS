"""The audit harness is part of the test suite — the ratchet is enforced by CI.

Two things are checked here:
  1. the repo currently passes its own audit (no unexpected drift);
  2. the harness actually *detects* drift — a check that cannot fail is prose,
     so we inject synthetic drift and require the corresponding check to fire.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import audit  # noqa: E402


class AuditTest(unittest.TestCase):
    def test_repo_passes_its_own_audit(self):
        report = audit.run_audit()
        if not report["ok"]:
            details = "\n".join(f"  {f['severity']:<7} {f['id']}: {f['title']} ({f['detail']})"
                                for f in report["unexpected"])
            self.fail(f"unexpected audit findings (fix or declare in ontology/known-gaps.json):\n"
                      f"{details}")

    def test_known_gaps_have_no_stale_entries(self):
        """The gap ledger may only shrink: a fixed gap must be removed from it."""
        report = audit.run_audit()
        self.assertEqual(report["resolved_known_gaps"], [],
                         "known gaps that no longer fire must be deleted from "
                         "ontology/known-gaps.json")

    def test_ontology_is_valid_json_and_complete(self):
        ontology = audit.load_ontology()
        for key in ("planes", "entities", "transitions", "invariants", "capabilities", "claims"):
            self.assertIn(key, ontology, f"ontology is missing '{key}'")
        for entity in ontology["entities"]:
            self.assertTrue(entity.get("plane"), f"{entity['name']} has no plane")
            self.assertTrue(entity.get("module"), f"{entity['name']} has no module")
        for invariant in ontology["invariants"]:
            self.assertTrue(invariant.get("enforced_by"), invariant["id"])
            self.assertTrue(invariant.get("tested_by"), invariant["id"])

    # ------------------------------------------------------------------ negative tests

    def test_states_check_detects_drift(self):
        """Inject a wrong transition table and require the check to fire."""
        ontology = audit.load_ontology()
        ontology["transitions"]["Workflow"] = {"pending": ["succeeded"]}  # nonsense
        findings = audit.check_states({"ontology": ontology})
        self.assertTrue(any(f.id == "C-states:drift:Workflow" for f in findings),
                        [f.id for f in findings])

    def test_route_check_detects_undocumented_routes(self):
        """The docs check compares both directions; a bogus doc row must fail."""
        original = audit.API_DOC.read_text(encoding="utf-8")
        try:
            audit.API_DOC.write_text(original + "\n| GET | `/definitely-not-a-real-route` | x |\n",
                                     encoding="utf-8")
            findings = audit.check_routes({"ontology": audit.load_ontology()})
        finally:
            audit.API_DOC.write_text(original, encoding="utf-8")
        self.assertTrue(any("C-routes:missing" in f.id for f in findings),
                        [f.id for f in findings])

    def test_capability_check_detects_a_missing_implementation(self):
        """Every compiler capability must be covered or explicitly unimplemented."""
        ontology = audit.load_ontology()
        for cap in ontology["capabilities"]:
            if cap["id"] == "cap://software/testing/execute":
                cap["satisfied_by"] = []  # pretend the local runtime vanished
        findings = audit.check_capabilities({"ontology": ontology})
        self.assertTrue(any(f.id == "C-caps:drift:cap://software/testing/execute"
                            for f in findings), [f.id for f in findings])

    def test_version_check_detects_a_stale_version(self):
        """Versions must be declared once; a drifting literal must fail the audit."""
        init = ROOT / "src" / "effective_scale" / "__init__.py"
        original = init.read_text(encoding="utf-8")
        try:
            init.write_text(original.replace('__version__ = "0.5.0"', '__version__ = "0.0.1"'),
                            encoding="utf-8")
            findings = audit.check_versions({"ontology": audit.load_ontology()})
        finally:
            init.write_text(original, encoding="utf-8")
        self.assertTrue(any(f.id == "C-versions:kernel" for f in findings), [f.id for f in findings])

    def test_version_check_detects_a_hardcoded_literal(self):
        ghost = ROOT / "src" / "effective_scale" / "ghost_version.py"
        ghost.write_text('SCHEMA = "9.9.9"\n', encoding="utf-8")
        try:
            findings = audit.check_versions({"ontology": audit.load_ontology()})
        finally:
            ghost.unlink()
        self.assertTrue(any("ghost_version" in f.id for f in findings), [f.id for f in findings])

    def test_dormant_entries_that_no_longer_describe_reality_are_reported(self):
        """Dormancy is a schedule, not a state: the ledger must shrink when modules move."""
        import json as _json

        ontology = audit.load_ontology()
        original = list(ontology["dormant"])
        try:
            # (a) an entry whose file is gone
            ontology["dormant"] = original + [{"module": "src/effective_scale/ghost.py",
                                               "reason": "deleted long ago",
                                               "revisit": "never"}]
            missing = audit.check_dead_modules({"ontology": ontology})
            self.assertTrue(any("C-dormant:missing" in f.id for f in missing),
                            [f.id for f in missing])

            # (b) an entry for a module that is now wired into production
            ontology["dormant"] = original + [{"module": "src/effective_scale/core/scheduler.py",
                                               "reason": "was unused once",
                                               "revisit": "now"}]
            stale = audit.check_dead_modules({"ontology": ontology})
            self.assertTrue(any("C-dormant:stale" in f.id for f in stale), [f.id for f in stale])
        finally:
            _json.dumps(original)  # untouched on disk; nothing to restore

    def test_operations_check_detects_a_missing_probe(self):
        """Supervision wiring is a requirement, not a nicety: remove it and the audit must fire."""
        compose = ROOT / "docker-compose.yml"
        original = compose.read_text(encoding="utf-8")
        try:
            compose.write_text(original.replace("/v1/health/ready", "/v1/health/nope"),
                               encoding="utf-8")
            findings = audit.check_operations({})
        finally:
            compose.write_text(original, encoding="utf-8")
        self.assertTrue(any(f.id == "C-ops:docker-compose.yml" for f in findings),
                        [f.id for f in findings])

    def test_version_check_detects_a_stale_manifest_tag(self):
        """Deploy manifests pin the product version too: a stale image tag must fail."""
        ghost = ROOT / "deploy" / "k8s" / "99-ghost.yaml"
        ghost.write_text("image: effective-scale-os:0.0.1\n", encoding="utf-8")
        try:
            findings = audit.check_versions({"ontology": audit.load_ontology()})
        finally:
            ghost.unlink()
        self.assertTrue(any("99-ghost.yaml" in f.id for f in findings), [f.id for f in findings])

    def test_deadcode_check_detects_an_undeclared_module(self):
        """A source file nobody imports and nobody declares must be reported."""
        ghost = ROOT / "src" / "effective_scale" / "ghost_module.py"
        ghost.write_text("VALUE = 1\n", encoding="utf-8")
        try:
            findings = audit.check_dead_modules({"ontology": audit.load_ontology()})
        finally:
            ghost.unlink()
        self.assertTrue(any("ghost_module" in f.id for f in findings), [f.id for f in findings])

    def test_stdlib_check_detects_a_third_party_import(self):
        ghost = ROOT / "src" / "effective_scale" / "ghost_import.py"
        ghost.write_text("import requests\n", encoding="utf-8")
        try:
            findings = audit.check_stdlib_only({})
        finally:
            ghost.unlink()
        self.assertTrue(any("requests" in f.id for f in findings), [f.id for f in findings])


class OntologyRenderTest(unittest.TestCase):
    def test_rendered_ontology_is_current(self):
        """docs/11-ontology.md must match ontology/system.json (regenerate, don't hand-edit)."""
        rendered = audit.render_ontology()
        target = ROOT / "docs" / "11-ontology.md"
        self.assertTrue(target.exists(), "run: python3 tools/audit.py --render-ontology")
        body = "\n".join(line for line in target.read_text(encoding="utf-8").splitlines()
                          if not line.startswith("<!--"))
        self.assertEqual(body.strip(), rendered.strip(),
                         "docs/11-ontology.md is stale: run python3 tools/audit.py --render-ontology")

    def test_known_gaps_file_matches_findings(self):
        known = json.loads((ROOT / "ontology" / "known-gaps.json").read_text())
        ids = {g["id"] for g in known["gaps"]}
        live = {f["id"] for f in audit.run_audit()["findings"]}
        self.assertTrue(ids <= live, f"known gaps not currently firing: {sorted(ids - live)}")


if __name__ == "__main__":
    unittest.main()
