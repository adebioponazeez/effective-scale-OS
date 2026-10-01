"""Composition root: registry, resolver, executor — one place wires the fabric."""
from __future__ import annotations

from pathlib import Path

from saf.agents.cli import (AiderAdapter, CodexCliAdapter, CursorCliAdapter,
                            OpenCodeAdapter, PiAdapter)
from saf.agents.git import GitRuntime
from saf.agents.web import WebRuntime
from saf.core.netpolicy import NetworkPolicy
from saf.agents.local import LocalRuntime
from saf.core.registry import Registry
from saf.core.resolver import CapabilityResolver
from saf.evidence.ledger import EvidenceLedger
from saf.evidence.verification import VerificationEngine
from saf.memory.store import MemoryStore
from saf.runtime.executor import CapabilityExecutor
from saf.runtime.stats import ResourceStats
from saf.tools.backup import BackupStore


def build_registry(test_command: list[str] | None = None) -> Registry:
    r = Registry()
    for a in [PiAdapter(), CursorCliAdapter(), CodexCliAdapter(), OpenCodeAdapter(), AiderAdapter()]:
        r.register(a.resource_id, a)
    local = LocalRuntime(test_command=test_command)
    r.register(local.resource_id, local)
    git = GitRuntime()
    r.register(git.resource_id, git)
    # Deny by default: with no SAF_NETWORK_ALLOW the runtime is registered but inert, and
    # reports that the policy is unconfigured instead of reaching out.
    web = WebRuntime(NetworkPolicy.from_env())
    r.register(web.resource_id, web)
    for provider in build_providers():
        r.register(provider.resource_id, provider)
    return r


def build_providers(env=None) -> list:
    """Model providers are registered only when their credentials exist.

    A provider without a key cannot generate anything, so registering it would only put a
    permanently-failing candidate in front of every resolution. The registry therefore
    reflects reality: no key, no candidate (see ontology dormant inventory).
    """
    import os

    environ = env if env is not None else os.environ
    providers = []
    if environ.get("OPENROUTER_API_KEY"):
        from saf.models.openrouter import OpenRouterAdapter

        providers.append(OpenRouterAdapter())
    return providers


def build_resolver(test_command: list[str] | None = None) -> CapabilityResolver:
    return CapabilityResolver(build_registry(test_command=test_command))


def build_executor(workspace: str = ".", *, state_dir: str = ".saf",
                   test_command: list[str] | None = None,
                   platform: str = "unknown") -> CapabilityExecutor:
    state = Path(state_dir)
    registry = build_registry(test_command=test_command)
    ledger = EvidenceLedger(str(state / "evidence.jsonl"))
    return CapabilityExecutor(
        CapabilityResolver(registry),
        workspace=workspace,
        ledger=ledger,
        verifier=VerificationEngine(ledger),
        memory=MemoryStore(str(state / "memory.jsonl")),
        backups=BackupStore(str(state / "backups")),
        resource_stats=ResourceStats(str(state / "resource-stats.jsonl")),
        platform=platform,
    )
