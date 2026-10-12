"""Deterministic research runtime (`saf://web`) — `cap://research/web`.

Scope, stated honestly: this fetches **caller-supplied URLs** through the network policy and
returns content hashes plus a bounded excerpt. It is not a search engine and does not crawl.
That is deliberate — a fetch with a hash is verifiable evidence; "research" that invents
findings is not.

Contract:
  * `constraints["url"]` or `constraints["urls"]` (max 10 per step) name the targets;
  * every URL is checked by `NetworkPolicy.check_url` **before** the request, and every
    redirect hop is checked again (a permitted host cannot bounce the fetch somewhere else);
  * the response body is read with a hard byte cap (`max_bytes`); oversized bodies are
    truncated and marked as such rather than buffered;
  * nothing is written to disk — evidence carries url, status, content type, byte count,
    sha256 and a bounded excerpt.

The policy is deny-by-default: with no allowlist, the runtime reports that it is
unconfigured instead of reaching out (see `NetworkPolicy`).
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from typing import Iterable

from .base import AgentRuntime
from saf.core.contracts import AgentResult
from saf.core.netpolicy import NetworkPolicy

WEB_CAPABILITIES = ["cap://research/web"]

MAX_URLS_PER_STEP = 10
EXCERPT_CHARS = 600


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop: the allowlist must hold end to end."""

    def __init__(self, policy: NetworkPolicy):
        super().__init__()
        self.policy = policy
        self.remaining = policy.max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if self.remaining <= 0:
            raise urllib.error.HTTPError(newurl, code, "too many redirects", headers, fp)
        allowed, reason = self.policy.check_url(newurl)
        if not allowed:
            raise urllib.error.HTTPError(newurl, code, f"redirect blocked: {reason}", headers, fp)
        self.remaining -= 1
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WebRuntime(AgentRuntime):
    """Fetch-and-hash runtime guarded by an explicit network policy."""

    resource_id = "saf://web"
    kind = "agent"
    capability_ids = WEB_CAPABILITIES
    trust = 0.5
    cost_estimate = 0.0
    latency_estimate_ms = 500

    def __init__(self, policy: NetworkPolicy | None = None):
        self.policy = policy or NetworkPolicy()

    async def execute(self, request) -> AgentResult:
        constraints = dict(request.task.constraints or {})
        urls = _requested_urls(constraints)
        if not urls:
            return AgentResult(
                ok=False,
                summary="no target urls: pass constraints.urls (this runtime fetches given "
                        "urls; it is not a search engine)",
                evidence=[{"kind": "web-fetch", "urls": [], "policy": self.policy.describe()}])
        if len(urls) > MAX_URLS_PER_STEP:
            return AgentResult(
                ok=False,
                summary=f"too many urls: {len(urls)} > {MAX_URLS_PER_STEP} per step",
                evidence=[{"kind": "web-fetch", "error": "too_many_urls",
                           "count": len(urls)}])
        if not self.policy.configured:
            return AgentResult(
                ok=False,
                summary="network policy is empty: set SAF_NETWORK_ALLOW (comma-separated hosts) "
                        "to permit outbound fetches",
                evidence=[{"kind": "web-fetch", "error": "network_policy_unconfigured",
                           "policy": self.policy.describe()}])
        if not request.context.network_available:
            return AgentResult(
                ok=False,
                summary="execution context declares network_available=False",
                evidence=[{"kind": "web-fetch", "error": "network_unavailable"}])

        entries, failures = [], 0
        for url in urls:
            entry = self._fetch(url)
            entries.append(entry)
            if not entry["ok"]:
                failures += 1
        ok_count = len(entries) - failures
        summary = f"fetched {ok_count}/{len(entries)} url(s)"
        if failures:
            first = next(e for e in entries if not e["ok"])
            summary += f"; first failure: {first['error']}"
        return AgentResult(
            ok=failures == 0,
            summary=summary,
            stdout="\n\n".join(e.get("excerpt", "") for e in entries if e.get("excerpt"))[:4000],
            artifacts=[e["url"] for e in entries if e["ok"]],
            evidence=[{"kind": "web-fetch", "policy": self.policy.describe(), "results": entries}])

    # ------------------------------------------------------------------ one request

    def _fetch(self, url: str) -> dict:
        allowed, reason = self.policy.check_url(url)
        if not allowed:
            return {"url": url, "ok": False, "error": f"policy: {reason}"}

        handler = _PolicyRedirectHandler(self.policy)
        opener = urllib.request.build_opener(handler)
        request = urllib.request.Request(
            url, headers={"User-Agent": "sovereign-agent-fabric/0.2 (+research)",
                          "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5"})
        try:
            with opener.open(request, timeout=self.policy.timeout_s) as response:
                status = getattr(response, "status", 200)
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(self.policy.max_bytes + 1)
                final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            return {"url": url, "ok": False, "error": f"http {exc.code}", "status": exc.code}
        except urllib.error.URLError as exc:
            return {"url": url, "ok": False, "error": f"network error: {exc.reason}"}
        except (TimeoutError, OSError, ValueError) as exc:
            return {"url": url, "ok": False, "error": f"request failed: {type(exc).__name__}"}

        truncated = len(raw) > self.policy.max_bytes
        body = raw[:self.policy.max_bytes] if truncated else raw
        digest = hashlib.sha256(body).hexdigest()
        text = _decode(body, content_type)
        return {
            "url": url,
            "final_url": final_url if final_url != url else None,
            "ok": 200 <= status < 300,
            "status": status,
            "content_type": content_type,
            "bytes": len(body),
            "truncated": truncated,
            "sha256": digest,
            "excerpt": text[:EXCERPT_CHARS],
        }


def _requested_urls(constraints: dict) -> list[str]:
    raw: Iterable = []
    if constraints.get("urls"):
        raw = constraints["urls"] if isinstance(constraints["urls"], (list, tuple)) else [constraints["urls"]]
    elif constraints.get("url"):
        raw = [constraints["url"]]
    seen, urls = set(), []
    for item in raw:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            urls.append(value)
    return urls


def _decode(body: bytes, content_type: str) -> str:
    if "text" not in content_type and "json" not in content_type and "xml" not in content_type \
            and content_type:
        return f"<{len(body)} bytes of {content_type.split(';')[0]}>"
    return body.decode("utf-8", errors="replace")
