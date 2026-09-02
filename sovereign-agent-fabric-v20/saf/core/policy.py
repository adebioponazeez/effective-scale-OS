import re

# Exact original policy set (kept verbatim, unchanged from the source):
# only the matching is hardened to word boundaries so "delete" no longer
# matches "undelete" and "deploy" no longer matches "redeploy"-adjacent words.
DESTRUCTIVE = (
    r"\bdelete\b",
    r"\bdestroy\b",
    r"\bdrop\b",
    r"\bwipe\b",
    r"\btransfer\b",
    r"\bpurchase\b",
    r"\bdeploy\b",
)

# A2 planning alone cannot execute side effects; only A3+ execution autonomy
# requires explicit human approval for destructive operations (docs §18).
EXECUTING_AUTONOMY = {"A3", "A4", "A5"}


class PolicyEngine:
    def authorize(self, task):
        text = task.intent.lower()
        if any(re.search(verb, text) for verb in DESTRUCTIVE) and task.autonomy.value in EXECUTING_AUTONOMY:
            return False, "Explicit human approval required."
        return True, "allowed"
