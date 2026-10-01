import re

from .contracts import Task

# Word-boundary matching: "test" must not match "latest/contest", "git" must
# not match "digit". Keywords map to canonical cap:// identities only.
KEYWORDS = {
    r"\brepos?": ["cap://software/repository/inspect"],
    r"\brefactor(?:ing)?\b": ["cap://software/code/refactor"],
    r"\btests?\b": ["cap://software/testing/execute"],
    r"\bgit\b": ["cap://software/git/operate"],
    r"\bcommit\b": ["cap://software/git/operate"],
    r"\bresearch\b": ["cap://research/web"],
    r"\bbrowser\b": ["cap://interaction/browser"],
    r"\bverify(?:ing|ied|y)?\b": ["cap://verification/save-proof"],
}

_FALLBACK = ["cap://general/agent/execute"]

# A commit message may be carried explicitly as a quoted segment: git commit "fix: thing".
# Only the first quoted segment is used, it is length-bounded, and it is passed to git as a
# single argv element (see saf/agents/git.py) — never interpolated into a shell.
_MESSAGE_RE = re.compile(r"""["']([^"']{1,200})["']""")


def compile_intent(intent: str) -> Task:
    text = intent.lower()
    caps = []
    for pattern, ids in KEYWORDS.items():
        if re.search(pattern, text):
            caps.extend(ids)
    capabilities = list(dict.fromkeys(caps or _FALLBACK))
    constraints: dict = {}
    if "cap://software/git/operate" in capabilities:
        match = _MESSAGE_RE.search(intent)
        if match:
            constraints["message"] = match.group(1).strip()
    return Task(
        intent=intent,
        required_capabilities=capabilities,
        constraints=constraints,
    )
