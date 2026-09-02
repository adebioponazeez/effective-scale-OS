from abc import ABC, abstractmethod

class AgentRuntime(ABC):
    resource_id = "agent://unknown"
    kind = "agent"
    capability_ids = []
    trust = 0.5
    cost_estimate = 0.0
    latency_estimate_ms = 1000

    @abstractmethod
    async def execute(self, request):
        raise NotImplementedError
