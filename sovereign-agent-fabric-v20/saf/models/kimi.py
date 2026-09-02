from .base import ModelProvider
from saf.core.contracts import ModelResponse

class KimiK3Adapter(ModelProvider):
    resource_id="model://kimi-k3"
    capability_ids=["cap://model/reasoning","cap://model/coding","cap://model/long-context"]
    async def generate(self, request):
        return ModelResponse(ok=False, text="Kimi K3 adapter boundary ready; connect direct or local inference.", model=request.model or "kimi-k3")
