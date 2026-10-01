#!/usr/bin/env python3
"""Repo audit harness — checks the code against the ontology and the docs.

Why this exists: every audit before this one was *prose*. The hardening records
(`docs/05-review-and-hardening.md`, F1–F15 / N1–N8) were hand-maintained tables:
correct on the day they were written, stale the day after, and unable to fail.
This tool makes the same classes of claim executable:

  * docs  ->  code   (API reference routes vs registered routes)
  * ontology -> code (states, transitions, entities, persisters)
  * registry -> ontology (capabilities and the resources that satisfy them)
  * reachability    (modules nobody imports must be declared dormant)
  * claims          (stdlib-only kernel, invariant enforcement points exist)

Findings are reported with a stable id. Anything not listed in
`ontology/known-gaps.json` fails the audit (exit 1) — so the repo can only drift
in ways somebody wrote down on purpose. `tests/test_audit.py` runs the same
harness inside the kernel suite, which means the ratchet is enforced by CI.

Usage:
    python3 tools/audit.py                 # human report, exit 1 on new findings
    python3 tools/audit.py --json          # machine report (audit.json shape)
    python3 tools/audit.py --update-gaps   # rewrite known-gaps.json with current findings
    python3 tools/audit.py --render-ontology   # markdown view of ontology/system.json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SAF = ROOT / "sovereign-agent-fabric-v20"
ONTOLOGY = ROOT / "ontology" / "system.json"
KNOWN_GAPS = ROOT / "ontology" / "known-gaps.json"
API_DOC = ROOT / "docs" / "09-api-reference.md"

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Finding:
    check: str
    id: str
    severity: str
    title: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"check": self.check, "id": self.id, "severity": self.severity,
                "title": self.title, "detail": self.detail, "evidence": self.evidence}


# --------------------------------------------------------------------------- utils

def load_ontology() -> dict:
    return json.loads(ONTOLOGY.read_text(encoding="utf-8"))


def load_known_gaps() -> dict[str, dict]:
    if not KNOWN_GAPS.exists():
        return {}
    data = json.loads(KNOWN_GAPS.read_text(encoding="utf-8"))
    return {g["id"]: g for g in data.get("gaps", [])}


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py")
                  if "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
                  and "egg-info" not in str(p))


def module_name(path: Path, package_root: Path, package: str) -> str:
    rel = path.relative_to(package_root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    if parts and parts[0] == package:      # path already rooted at the package dir
        parts = parts[1:]
    return ".".join([package, *parts]) if parts else package


def iter_imports(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                out.append(node.module)
    return out


# --------------------------------------------------------------------------- checks

def check_stdlib_only(ctx) -> list[Finding]:
    """C-stdlib: the reference kernel is importable with no third-party packages."""
    findings = []
    allowed = set(sys.stdlib_module_names)
    for path in python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in iter_imports(tree):
            top = name.split(".")[0]
            if top in allowed or top == "effective_scale":
                continue
            findings.append(Finding(
                "stdlib_only", f"C-stdlib:{path.relative_to(ROOT)}:{top}", "high",
                "kernel source imports a third-party module",
                f"{path.relative_to(ROOT)} imports '{name}'",
            ))
    return findings


def _registered_routes() -> tuple[set[str], set[str]]:
    """Capture the routes the server actually registers, without starting it."""
    sys.path.insert(0, str(SRC))
    from effective_scale.adapters import MemoryStore
    from effective_scale.api.server import ApiServer
    from effective_scale.core.kernel import Config, Kernel
    from effective_scale.ports.logger import MemLogger

    kernel = Kernel(MemoryStore(), config=Config(store_path=":memory:",
                                                  auth_secret="x" * 20),
                    logger=MemLogger())
    api = ApiServer(kernel)
    seen: list[tuple[str, str]] = []
    original = api.route

    def spy(methods, pattern, fn, **kw):
        seen.append((methods, pattern))
        return original(methods, pattern, fn, **kw)

    api.route = spy  # type: ignore[method-assign]
    api._registered()
    registered = {f"{m} {p}" for methods, p in seen for m in methods.split(",")}
    patterns = {p for _m, p in seen}
    return registered, patterns


def _documented_routes() -> set[str]:
    doc = API_DOC.read_text(encoding="utf-8")
    out = set()
    for line in doc.splitlines():
        row = re.match(r"\|\s*(GET|POST|PUT|DELETE)\s*\|\s*(.+?)\s*\|", line)
        if not row:
            continue
        method, cell = row.group(1), row.group(2)
        for raw in re.findall(r"`([^`]+)`", cell):
            path = raw.split("?")[0].strip()
            if not path.startswith("/"):
                continue
            if not path.startswith("/v1"):
                path = "/v1" + path
            out.add(f"{method} {path}")
    return out


def check_routes(ctx) -> list[Finding]:
    """C-routes: docs/09 and the router agree, in both directions."""
    findings = []
    registered, _patterns = _registered_routes()
    documented = _documented_routes()
    by_path = {r.split(" ", 1)[1]: r for r in registered}
    for doc in sorted(documented - registered):
        if doc.split(" ", 1)[1] in by_path:
            continue  # same path, different method spelling: reported below
        findings.append(Finding(
            "routes", f"C-routes:missing:{doc}", "high",
            "documented route is not registered", doc))
    for reg in sorted(registered - documented):
        findings.append(Finding(
            "routes", f"C-routes:undocumented:{reg}", "medium",
            "registered route is not in the API reference", reg))
    return findings


def _code_state_tables() -> dict[str, dict]:
    sys.path.insert(0, str(SRC))
    from effective_scale.domain import states as S
    enums = {}
    for name, table in (("Workflow", S._WF), ("WorkflowNode", S._NODE),
                        ("Attempt", S._ATTEMPT), ("Lease", S._LEASE),
                        ("EventMsg", S._EVENT)):
        enums[name] = {k.value: sorted(v.value for v in vs) for k, vs in table.items() if vs}
    return enums


def check_states(ctx) -> list[Finding]:
    """C-states: ontology transition tables are exactly the code tables."""
    findings = []
    code = _code_state_tables()
    ont = ctx["ontology"].get("transitions", {})
    for name in sorted(set(code) | set(ont)):
        if name not in ont:
            findings.append(Finding("states", f"C-states:missing:{name}", "high",
                                    "state machine in code but not in the ontology", name))
            continue
        if name not in code:
            findings.append(Finding("states", f"C-states:extra:{name}", "medium",
                                    "state machine declared in the ontology but not in code", name))
            continue
        def _norm(table: dict) -> dict:
            return {k: sorted(v) for k, v in table.items() if v}

        if _norm(code[name]) != _norm(ont[name]):
            findings.append(Finding(
                "states", f"C-states:drift:{name}", "high",
                f"transition table drift for {name}",
                f"code={code[name]} ontology={ont[name]}"))
    # every declared state must exist in its enum
    from effective_scale.domain import states as S
    enums = {"Workflow": S.WorkflowStatus, "WorkflowNode": S.NodeStatus,
             "Attempt": S.AttemptStatus, "Lease": S.LeaseState, "EventMsg": S.EventStatus,
             "Node": S.NodeState, "Workload": S.WorkloadStatus}
    for entity in ctx["ontology"]["entities"]:
        enum_name = entity.get("states_enum")
        if not enum_name or enum_name not in {e.__name__ for e in enums.values()}:
            continue
        enum = next(e for e in enums.values() if e.__name__ == enum_name)
        declared = set(entity.get("states", []))
        actual = {m.value for m in enum}
        if declared != actual:
            findings.append(Finding("states", f"C-states:enum:{enum_name}", "high",
                                    f"{entity['name']} states do not match {enum_name}",
                                    f"declared={sorted(declared)} code={sorted(actual)}"))
    return findings


def check_persisters(ctx) -> list[Finding]:
    """C-persist: every persisted entity has a store persister method."""
    findings = []
    store_src = (SRC / "effective_scale" / "ports" / "store.py").read_text(encoding="utf-8")
    for entity in ctx["ontology"]["entities"]:
        table = entity.get("store_table")
        if not table:
            continue
        explicit = {
            "WorkflowNode": {"put_workflow"},      # nodes live inside the workflow record
            "AuditEntry": {"put_audit"},
            "DlqEntry": {"put_dlq"},
            "EventMsg": {"put_event"},
            "IdempotencyKey": {"put_idempotency"},
        }
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", entity["name"]).lower()
        candidates = explicit.get(entity["name"],
                                  {f"put_{snake}", f"put_{snake}s", f"put_{snake}_entry"})
        if not any(c in store_src for c in candidates):
            findings.append(Finding("persisters", f"C-persist:{entity['name']}", "medium",
                                    f"no store persister found for {entity['name']}",
                                    f"looked for {sorted(candidates)}; table={table}"))
    return findings


def _registry_resources() -> dict[str, list[str]]:
    """resource_id -> capability_ids, from the SAF composition root."""
    sys.path.insert(0, str(SAF))
    from saf.runtime.bootstrap import build_registry

    registry = build_registry()
    cap_map: dict[str, list[str]] = {}
    for resource in registry.all():
        for cap in getattr(resource, "capability_ids", []):
            cap_map.setdefault(cap, []).append(resource.resource_id)
    return cap_map


def _compiler_capabilities() -> set[str]:
    sys.path.insert(0, str(SAF))
    from saf.core.compiler import KEYWORDS, _FALLBACK

    caps: set[str] = set(_FALLBACK)
    for ids in KEYWORDS.values():
        caps.update(ids)
    return caps


def check_capabilities(ctx) -> list[Finding]:
    """C-caps: capabilities the compiler can emit are satisfied or declared unimplemented."""
    findings = []
    live = _registry_resources()
    declared = {c["id"]: c for c in ctx["ontology"]["capabilities"]}

    for cap in sorted(_compiler_capabilities()):
        entry = declared.get(cap)
        if entry is None:
            findings.append(Finding("capabilities", f"C-caps:undeclared:{cap}", "high",
                                    "compiler emits a capability the ontology never declares", cap))
            continue
        expected = sorted(entry.get("satisfied_by", []))
        actual = sorted(live.get(cap, []))
        if expected != actual:
            findings.append(Finding(
                "capabilities", f"C-caps:drift:{cap}", "high",
                f"resource coverage drift for {cap}",
                f"ontology={expected} registry={actual}"))
        if entry.get("unimplemented"):
            findings.append(Finding(
                "capabilities", f"C-caps:unimplemented:{cap}", "medium",
                f"declared capability has no implementation ({cap})",
                entry.get("note", "")))

    for cap, entry in sorted(declared.items()):
        if cap not in _compiler_capabilities() and not entry.get("satisfied_by"):
            findings.append(Finding("capabilities", f"C-caps:orphan:{cap}", "low",
                                    f"capability declared but unreachable from the compiler", cap))
    return findings


def _all_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in python_files(SAF / "saf"):
        modules[module_name(path, SAF, "saf")] = path
    for path in python_files(SRC):
        name = module_name(path, SRC, "effective_scale")
        if name.count(".") <= 3:
            modules[name] = path
    return modules


def _import_graph() -> tuple[dict[str, set[str]], dict[str, Path]]:
    """module -> imported modules, over kernel + SAF + tests. Relative imports resolved."""
    modules = _all_modules()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    by_path = {path: name for name, path in modules.items()}
    files = python_files(SAF / "saf") + python_files(SRC)
    test_files = python_files(ROOT / "tests") + python_files(SAF / "tests")

    def resolve(path: Path, node: ast.ImportFrom) -> str | None:
        own = by_path.get(path, "")
        if path.name == "__init__.py":
            package = own.split(".")        # a package's own name IS its directory
        else:
            package = own.split(".")[:-1]
        if node.level:
            depth = node.level - 1
            base = ".".join(package[:len(package) - depth]) if depth else ".".join(package)
            return f"{base}.{node.module}" if node.module else base
        return node.module

    for path in files + test_files:
        own = by_path.get(path)
        if own is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                target = resolve(path, node)
                if target:
                    targets.append(target)
                    targets.extend(f"{target}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                targets.extend(a.name for a in node.names)
            for target in targets:
                # link to the longest known module prefix
                parts = target.split(".")
                for i in range(len(parts), 0, -1):
                    candidate = ".".join(parts[:i])
                    if candidate in modules:
                        graph[own].add(candidate)
                        break
    return graph, modules


def _reachable(graph: dict[str, set[str]], roots: set[str]) -> set[str]:
    seen: set[str] = set()
    stack = [r for r in roots if r in graph]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return seen


def check_dead_modules(ctx) -> list[Finding]:
    """C-deadcode: from the entry points, every module must be reachable or declared dormant."""
    findings = []
    dormant = {d["module"]: d for d in ctx["ontology"].get("dormant", [])}
    graph, modules = _import_graph()

    prod_roots = {"effective_scale.__main__", "effective_scale.main",
                  "saf.cli.main", "saf.runtime.bootstrap"}
    test_roots = {name for name, path in modules.items()
                  if "tests" in path.parts and path.name.startswith("test_")}
    prod_reachable = _reachable(graph, prod_roots)
    test_reachable = _reachable(graph, test_roots) - prod_reachable

    for name, path in sorted(modules.items()):
        if path.name == "__init__.py" or name in prod_roots:
            continue
        rel = str(path.relative_to(ROOT))
        if name in prod_reachable or rel in dormant:
            continue
        detail = ("reachable only from tests — nothing in production code uses it"
                  if name in test_reachable else "not reachable from any entry point")
        findings.append(Finding(
            "dead_modules", f"C-deadcode:{rel}", "medium",
            "module is not reachable from production entry points", f"{rel}: {detail}"))

    # Dormancy must stay honest: the ledger may only shrink, so an entry that no longer
    # describes reality (file gone, or module now wired into production) is a finding.
    by_rel = {str(path.relative_to(ROOT)): name for name, path in modules.items()}
    for rel, entry in sorted(dormant.items()):
        if not (ROOT / rel).exists():
            findings.append(Finding(
                "dead_modules", f"C-dormant:missing:{rel}", "high",
                "declared dormant but the file does not exist",
                f"{rel}: remove the entry or restore the module"))
            continue
        name = by_rel.get(rel)
        if name and name in prod_reachable:
            findings.append(Finding(
                "dead_modules", f"C-dormant:stale:{rel}", "medium",
                "declared dormant but reachable from production code",
                f"{rel} is wired into production; delete its dormant entry "
                f"(the inventory may only shrink)"))
    return findings


VERSION_RE = re.compile(r"""["'](\d+\.\d+\.\d+)["']""")
MANIFEST_VERSION_RE = re.compile(r"""(effective-scale-os:)(\d+\.\d+\.\d+)""")


def _declared_version(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _pyproject_version(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    match = re.search(r"""(?m)^version\s*=\s*["']([^"']+)["']""", path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def check_versions(ctx) -> list[Finding]:
    """One version per product, declared once. Duplicated literals are drift waiting to happen."""
    findings: list[Finding] = []
    ontology = ctx["ontology"]
    expected = ontology.get("product_version", {})
    allowed = set(ontology.get("version_literals_allowed", []))
    products = {
        "kernel": (ROOT / "src" / "effective_scale" / "__init__.py", ROOT / "pyproject.toml"),
        "saf": (ROOT / "sovereign-agent-fabric-v20" / "saf" / "__init__.py",
                ROOT / "sovereign-agent-fabric-v20" / "pyproject.toml"),
    }
    for name, (init_file, pyproject) in products.items():
        want = expected.get(name)
        have = _declared_version(init_file)
        packaged = _pyproject_version(pyproject)
        sources = {str(init_file.relative_to(ROOT)): have, str(pyproject.relative_to(ROOT)): packaged}
        disagree = {k: v for k, v in sources.items() if v != want}
        if disagree:
            findings.append(Finding(
                id=f"C-versions:{name}", check="versions", severity="medium",
                title=f"{name} version does not match the ontology",
                detail=f"ontology says {want}; " + ", ".join(f"{k}={v}" for k, v in disagree.items()),
                evidence=[str(init_file.relative_to(ROOT)), str(pyproject.relative_to(ROOT))]))
    # deploy manifests carry the product version in image tags: same single-source rule
    kernel_version = expected.get("kernel")
    manifests = sorted((ROOT / "deploy").rglob("*.yaml")) + [ROOT / "docker-compose.yml"]
    for path in manifests:
        if not path.exists():
            continue
        rel = str(path.relative_to(ROOT))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = MANIFEST_VERSION_RE.search(line)
            if match and match.group(2) != kernel_version:
                findings.append(Finding(
                    id=f"C-versions:manifest:{rel}", check="versions", severity="medium",
                    title="deploy manifest pins a stale kernel image tag",
                    detail=f"{rel}:{lineno} pins {match.group(2)}, ontology says {kernel_version}",
                    evidence=[f"{rel}:{lineno}"]))
    for root in (ROOT / "src", ROOT / "sovereign-agent-fabric-v20" / "saf"):
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if path.name == "__init__.py" or rel in allowed or "__pycache__" in rel:
                continue
            match = VERSION_RE.search(path.read_text(encoding="utf-8"))
            if match:
                line = path.read_text(encoding="utf-8")[:match.start()].count("\n") + 1
                findings.append(Finding(
                    id=f"C-versions:hardcoded:{rel}", check="versions", severity="medium",
                    title="version literal hardcoded outside __init__.py",
                    detail=f"{rel}:{line} contains {match.group(1)}; import __version__ instead "
                           f"(or declare the file in ontology.version_literals_allowed with a reason)",
                    evidence=[f"{rel}:{line}"]))
    return findings


# Deployment artifacts must not be decoration: each one must say how the process comes back
# and how a supervisor knows it is healthy.
OPS_REQUIREMENTS = [
    ("deploy/systemd/effective-scale.service",
     ["[Service]", "Restart=always", "ExecStart=", "KillSignal=SIGTERM", "ReadWritePaths="],
     "unit must define restart policy, exec line, SIGTERM drain and its writable path"),
    ("deploy/k8s/03-deployment.yaml",
     ["startupProbe", "readinessProbe", "/v1/health/ready", "livenessProbe", "/v1/health/live",
      "terminationGracePeriodSeconds"],
     "manifest must wire probes to the real health routes and allow a graceful drain"),
    ("docker-compose.yml",
     ["healthcheck", "/v1/health/ready", "restart:"],
     "compose service must declare a healthcheck and a restart policy"),
    ("Dockerfile",
     ["HEALTHCHECK", "/v1/health/ready"],
     "image must declare a healthcheck against the readiness route"),
    ("deploy/windows/install-service.ps1",
     ["sc.exe failure", "New-Service", "Start-Service"],
     "service installer must set a failure/restart action"),
]


def check_operations(ctx) -> list[Finding]:
    """C-ops: supervision and probes exist for every runtime we ship."""
    findings: list[Finding] = []
    for rel, required, why in OPS_REQUIREMENTS:
        path = ROOT / rel
        if not path.exists():
            findings.append(Finding(
                id=f"C-ops:missing:{rel}", check="operations", severity="high",
                title="deployment artifact is missing entirely", detail=why, evidence=[rel]))
            continue
        text = path.read_text(encoding="utf-8")
        missing = [token for token in required if token not in text]
        if missing:
            findings.append(Finding(
                id=f"C-ops:{rel}", check="operations", severity="high",
                title="deployment artifact lacks supervision/probe wiring",
                detail=f"{rel} is missing {missing} — {why}", evidence=[rel]))
    return findings


def check_invariants(ctx) -> list[Finding]:
    """C-invariants: enforcement points and tests named by the ontology must exist."""
    findings = []
    for inv in ctx["ontology"]["invariants"]:
        for key in ("enforced_by", "tested_by"):
            value = inv.get(key, "")
            if "#" in value:  # self-referential claim, e.g. ontology#claims.C-stdlib
                continue
            if not value or not (ROOT / value).exists():
                findings.append(Finding(
                    "invariants", f"C-invariants:{inv['id']}:{key}", "high",
                    f"invariant {inv['id']} names a {key} path that does not exist", value))
    return findings


def _count_unittest_tests(root: Path) -> int:
    total = 0
    for path in sorted(root.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                total += 1
            elif isinstance(node, ast.ClassDef):
                base_ok = True
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test_") and base_ok:
                        total += 1
    return total


def check_test_counts(ctx) -> list[Finding]:
    """C-test-counts: numbers stated in the READMEs match discoverable tests."""
    findings = []
    kernel = _count_unittest_tests(ROOT / "tests")
    saf = _count_unittest_tests(SAF / "tests")
    claims = {
        "README.md": [(ROOT / "README.md").read_text(encoding="utf-8"), kernel],
        "sovereign-agent-fabric-v20/README.md":
            [(SAF / "README.md").read_text(encoding="utf-8"), saf],
    }
    for name, (text, actual) in claims.items():
        stated = {int(m) for m in re.findall(r"(\d+)\s+tests", text)}
        if stated and actual not in stated and (max(stated) if stated else 0) != actual:
            findings.append(Finding(
                "test_counts", f"C-test-counts:{name}", "low",
                "stated test count does not match discovery",
                f"{name} states {sorted(stated)}, discovery finds {actual}. State the real total "
                f"once (e.g. \"{actual} tests\"); spell out any other count in words "
                f"(\"four policy tests\") so this check stays unambiguous."))
    ctx["counts"] = {"kernel_tests": kernel, "saf_tests": saf}
    return findings


CHECKS = [check_stdlib_only, check_routes, check_states, check_persisters,
          check_versions, check_operations,
          check_capabilities, check_dead_modules, check_invariants, check_test_counts]


# --------------------------------------------------------------------------- runner

def run_audit() -> dict:
    ctx = {"ontology": load_ontology()}
    findings: list[Finding] = []
    for check in CHECKS:
        findings.extend(check(ctx))
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.check, f.id))
    known = load_known_gaps()
    unexpected = [f for f in findings if f.id not in known]
    resolved = [gid for gid in known if gid not in {f.id for f in findings}]
    return {
        "counts": ctx.get("counts", {}),
        "findings": [f.as_dict() for f in findings],
        "unexpected": [f.as_dict() for f in unexpected],
        "resolved_known_gaps": resolved,
        "ok": not unexpected,
    }


def render(report: dict) -> str:
    lines = ["# repo audit", ""]
    counts = report.get("counts", {})
    if counts:
        lines.append(f"discovered tests: kernel={counts.get('kernel_tests')} "
                     f"saf={counts.get('saf_tests')}")
        lines.append("")
    if not report["findings"]:
        lines.append("no findings — code, ontology and docs agree.")
    for finding in report["findings"]:
        mark = "NEW" if finding in report["unexpected"] else "known"
        lines.append(f"[{mark}] {finding['severity']:<7} {finding['id']}")
        lines.append(f"        {finding['title']}")
        if finding["detail"]:
            lines.append(f"        detail: {finding['detail']}")
    for gid in report["resolved_known_gaps"]:
        lines.append(f"[fixed] known gap no longer present: {gid} "
                     "(remove it from ontology/known-gaps.json)")
    lines.append("")
    lines.append(f"unexpected findings: {len(report['unexpected'])} "
                 f"| known gaps: {len(report['findings']) - len(report['unexpected'])}")
    return "\n".join(lines)


def update_gaps(report: dict) -> None:
    data = {"note": "Known gaps are audited findings that are accepted for now. "
                    "The list may only shrink: a check that no longer fires must be removed. "
                    "Anything not listed here fails tools/audit.py and tests/test_audit.py.",
            "gaps": [{"id": f["id"], "severity": f["severity"], "check": f["check"],
                      "title": f["title"], "detail": f["detail"]}
                     for f in report["findings"]]}
    KNOWN_GAPS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def render_ontology() -> str:
    ont = load_ontology()
    lines = ["# System ontology (generated from ontology/system.json)", ""]
    versions = ont.get("product_version", {})
    if versions:
        lines.append("Product versions: " + " · ".join(f"{k} `{v}`" for k, v in versions.items())
                     + " — declared once, enforced by `C-versions`.")
        lines.append("")
    lines.append("## Planes")
    lines.append("")
    lines.append("| Plane | Owned by | What it is |")
    lines.append("|---|---|---|")
    for plane in ont["planes"]:
        lines.append(f"| {plane['id']} | `{plane['owned_by']}` | {plane['description']} |")
    lines.append("")
    lines.append("## Entities")
    lines.append("")
    lines.append("| Entity | Plane | States | Identity | API |")
    lines.append("|---|---|---|---|---|")
    for e in ont["entities"]:
        states = ", ".join(e.get("states", [])) or "—"
        lines.append(f"| {e['name']} | {e['plane']} | {states} | {e.get('identity','')} "
                     f"| {', '.join(e.get('api', [])) or '—'} |")
    lines.append("")
    lines.append("## Invariants")
    lines.append("")
    lines.append("| # | Invariant | Enforced by | Tested by |")
    lines.append("|---|---|---|---|")
    for inv in ont["invariants"]:
        lines.append(f"| {inv['id']} | {inv['statement']} | `{inv['enforced_by']}` "
                     f"| `{inv['tested_by']}` |")
    lines.append("")
    lines.append("## Capabilities")
    lines.append("")
    lines.append("| Capability | Satisfied by | Deterministic |")
    lines.append("|---|---|---|")
    for cap in ont["capabilities"]:
        impl = ", ".join(cap.get("satisfied_by", [])) or "**none — declared unimplemented**"
        lines.append(f"| `{cap['id']}` | {impl} | {cap.get('deterministic', False)} |")
    lines.append("")
    lines.append("## Claims (each has a check that can fail — see `tools/audit.py`)")
    lines.append("")
    lines.append("| Claim | Statement | Check | Severity |")
    lines.append("|---|---|---|---|")
    for claim in ont.get("claims", []):
        lines.append(f"| `{claim['id']}` | {claim['statement']} | {claim['check']} | {claim['severity']} |")
    lines.append("")
    lines.append("## Dormant modules (declared, with a revisit trigger)")
    lines.append("")
    lines.append("| Module | Why it is dormant | Revisit when |")
    lines.append("|---|---|---|")
    for d in ont.get("dormant", []):
        lines.append(f"| `{d['module']}` | {d['reason']} | {d['revisit']} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--update-gaps", action="store_true")
    parser.add_argument("--render-ontology", action="store_true")
    args = parser.parse_args()

    if args.render_ontology:
        text = render_ontology()
        out = ROOT / "docs" / "11-ontology.md"
        header = ("<!-- GENERATED by `python3 tools/audit.py --render-ontology` — "
                  "edit ontology/system.json, not this file -->\n\n")
        out.write_text(header + text + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
        return 0

    report = run_audit()
    if args.update_gaps:
        update_gaps(report)
        print(f"wrote {KNOWN_GAPS.relative_to(ROOT)} "
              f"({len(report['findings'])} gaps, {len(report['unexpected'])} unexpected)")
        return 0
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
