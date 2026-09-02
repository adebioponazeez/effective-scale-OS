from .contracts import Candidate


class CapabilityResolver:
    """Rank resources for a task: fit, trust, cost, environment fit.

    Deterministic: ties keep registry (registration/insertion) order, so the
    same registry state always produces the same ranking. Score and confidence
    remain separate (docs/01 §9, README core law).
    """

    def __init__(self, registry):
        self.registry = registry

    def resolve(self, task, context):
        required = set(task.required_capabilities)
        network = bool(getattr(context, "network_available", True))
        out = []
        for item in self.registry.all():
            caps = set(getattr(item, "capability_ids", []))
            fit = len(required & caps) / max(1, len(required)) if required else 0.5
            trust = float(getattr(item, "trust", 0.5))
            cost = float(getattr(item, "cost_estimate", 0.0))
            score = 0.65 * fit + 0.25 * trust + 0.10 / (1.0 + cost)
            # Environment fit (docs §9): remote/model resources are penalized
            # when the network is known unavailable — deterministic, no state.
            if not network and getattr(item, "kind", "") in {"model", "model-provider"}:
                score -= 0.15
            out.append(Candidate(
                resource_id=item.resource_id, kind=item.kind,
                capabilities=list(caps), score=score, confidence=trust,
                cost_estimate=cost, latency_estimate_ms=getattr(item, "latency_estimate_ms", 1000),
            ))
        return sorted(out, key=lambda x: (x.score, x.confidence), reverse=True)
