from dataclasses import dataclass

@dataclass
class ResourceVector:
    tokens:float=0; compute:float=0; money:float=0; bandwidth:float=0
    storage:float=0; latency_ms:float=0; attention:float=0

def net_semantic_cost(current_cost,reusable_value):
    return current_cost-reusable_value
