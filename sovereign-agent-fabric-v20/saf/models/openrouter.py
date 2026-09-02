import asyncio
import json
import os
import urllib.error
import urllib.request

from .base import ModelProvider
from saf.core.contracts import ModelResponse


class OpenRouterAdapter(ModelProvider):
    """HTTPS model adapter with bounded, failure-tolerant calls.

    Hardening vs the NFRs (docs/02-engineering-brief.md):
    - the blocking urllib call runs in a worker thread so the event loop is
      never blocked (async I/O boundary);
    - provider errors (auth, 429, 5xx, network, malformed bodies) map to
      `ModelResponse(ok=False, ...)` instead of raising into the fabric;
    - timeout is explicit (`timeout_s`).
    """

    resource_id = "model-provider://openrouter"
    capability_ids = ["cap://model/reasoning", "cap://model/coding"]
    trust = 0.6
    timeout_s = 60.0
    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    async def generate(self, request):
        return await asyncio.to_thread(self._call, request)

    def _call(self, request) -> ModelResponse:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            return ModelResponse(
                ok=False,
                text="OPENROUTER_API_KEY not configured.",
                model=request.model or "openrouter",
            )
        payload = json.dumps({
            "model": request.model or "openai/gpt-5",
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
        }).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
            return ModelResponse(
                ok=True,
                text=data["choices"][0]["message"]["content"],
                model=data.get("model", request.model or "openrouter"),
                usage=data.get("usage", {}),
                raw=data,
            )
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")[:500]
            except OSError:
                pass
            return ModelResponse(
                ok=False,
                text=f"OPENROUTER HTTP {exc.code}: {detail or exc.reason}",
                model=request.model or "openrouter",
                raw={"http_error": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ModelResponse(
                ok=False,
                text=f"OPENROUTER unreachable: {exc}",
                model=request.model or "openrouter",
            )
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            return ModelResponse(
                ok=False,
                text=f"OPENROUTER malformed response: {exc}",
                model=request.model or "openrouter",
                raw={"parse_error": str(exc)},
            )
