from saf.core.registry import Registry
from saf.core.resolver import CapabilityResolver
from saf.agents.cli import PiAdapter,CursorCliAdapter,CodexCliAdapter,OpenCodeAdapter,AiderAdapter

def build_registry():
    r=Registry()
    for a in [PiAdapter(),CursorCliAdapter(),CodexCliAdapter(),OpenCodeAdapter(),AiderAdapter()]:
        r.register(a.resource_id,a)
    return r

def build_resolver():
    return CapabilityResolver(build_registry())
