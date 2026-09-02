from .base import ModelProvider
from saf.core.contracts import ModelResponse

class AbacusAIAdapter(ModelProvider):
    resource_id="provider://abacus-ai"
    capability_ids=["cap://agent/workflow","cap://model/reasoning","cap://tool/mcp"]
    async def generate(self, request):
        return ModelResponse(ok=False, text="Abacus.AI adapter boundary ready; configure provider API.", model=request.model or "abacus-ai")
