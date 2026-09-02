from abc import ABC, abstractmethod

class ModelProvider(ABC):
    resource_id="model://unknown"; kind="model"; capability_ids=[]; trust=0.5
    @abstractmethod
    async def generate(self, request):
        raise NotImplementedError
