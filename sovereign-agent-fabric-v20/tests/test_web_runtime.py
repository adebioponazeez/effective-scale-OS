"""Network policy + the research runtime (`cap://research/web` → `saf://web`).

Two layers of proof:

  * the **policy** is tested entirely offline — scheme, allowlist, SSRF addresses, ports and
    bounds — because a security boundary you can only test by connecting is not testable;
  * the **runtime** is tested against a real HTTP server bound to loopback, with a policy that
    permits exactly that host (and `allow_private` explicitly on). The same runtime is then
    shown to refuse the same URL when the policy does not permit it.

Redirect re-validation, byte caps, excerpt/hash evidence and the deny-by-default stance are all
asserted here.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import socket
import sys
import threading

import pytest

from saf.agents.web import EXCERPT_CHARS, MAX_URLS_PER_STEP, WebRuntime
from saf.core.contracts import AgentRequest, ExecutionContext, Task
from saf.core.netpolicy import NetworkPolicy

BODY = b"<html><body>sovereign research payload " + b"x" * 200 + b"</body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    redirect_to = "/final"
    status = 200

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        if self.path == "/escape":
            # bounce to a host the policy does not allow
            self.send_response(302)
            self.send_header("Location", "http://evil.invalid/steal")
            self.end_headers()
            return
        if self.path == "/big":
            payload = b"B" * 5000
        elif self.path == "/json":
            payload = b'{"ok": true, "value": 42}'
        elif self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        else:
            payload = BODY
        self.send_response(self.status)
        content_type = "application/json" if self.path == "/json" else "text/html"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test server
        return


@pytest.fixture()
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _policy(server, **overrides) -> NetworkPolicy:
    host, _, port = server.split("//")[1].partition(":")
    values = dict(allow_hosts=(host,), allow_schemes=("http",), allow_private=True,
                  allow_ports=(int(port),))
    values.update(overrides)
    return NetworkPolicy(**values)


def _request(url, *, workspace="/tmp", network=True, **constraints):
    return AgentRequest(
        task=Task(intent=f"research {url}", required_capabilities=["cap://research/web"],
                  constraints=constraints or {"url": url}),
        context=ExecutionContext(task_id="t", platform=sys.platform, workspace=workspace,
                                 network_available=network),
        prompt="research",
    )


# --------------------------------------------------------------------------- policy

class TestNetworkPolicy:
    def test_deny_by_default(self):
        policy = NetworkPolicy()
        allowed, reason = policy.check_url("https://example.com/")
        assert allowed is False
        assert "allowlist" in reason and "deny by default" in reason

    def test_scheme_allowlist(self):
        policy = NetworkPolicy(allow_hosts=("example.com",))
        assert policy.check_url("http://example.com/")[0] is False   # http not allowed by default
        assert policy.check_url("ftp://example.com/")[0] is False
        assert policy.check_url("https://example.com/")[0] is True

    def test_exact_host_and_subdomain_wildcard(self):
        """Rule lookup is DNS-free; the resolving decision is covered separately below."""
        policy = NetworkPolicy(allow_hosts=("example.com", "*.docs.example.org"))
        assert policy.host_permitted("example.com") is True
        assert policy.host_permitted("api.example.com") is False   # exact entries do not cascade
        assert policy.host_permitted("a.docs.example.org") is True
        # a wildcard must not match the bare domain
        assert policy.host_permitted("docs.example.org") is False
        assert policy.host_permitted("x.a.docs.example.org") is True

    def test_ports_are_restricted(self):
        policy = NetworkPolicy(allow_hosts=("127.0.0.1",), allow_schemes=("http",),
                               allow_private=True)
        assert policy.check_url("http://127.0.0.1:8443/")[0] is False
        assert policy.check_url("http://127.0.0.1:80/")[0] is True
        assert NetworkPolicy(allow_hosts=("127.0.0.1",), allow_schemes=("http",),
                             allow_private=True, allow_ports=(8443,)
                             ).check_url("http://127.0.0.1:8443/")[0] is True

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/",
        "https://localhost/",
        "https://[::1]/",
        "https://10.0.0.5/",
        "https://169.254.169.254/latest/meta-data/",   # cloud metadata service
    ])
    def test_ssrf_guard_refuses_non_public_addresses(self, url):
        host = url.split("//")[1].split("/")[0].strip("[]")
        policy = NetworkPolicy(allow_hosts=(host,), allow_schemes=("https",))
        allowed, reason = policy.check_url(url)
        assert allowed is False
        assert ("non-public" in reason) or ("dns resolution failed" in reason)

    def test_private_addresses_can_be_opted_into(self):
        policy = NetworkPolicy(allow_hosts=("127.0.0.1",), allow_schemes=("http",),
                               allow_private=True, allow_ports=(8080,))
        assert policy.check_url("http://127.0.0.1:8080/")[0] is True
        # ... and public resolution still happens once private targets are allowed
        assert policy.check_url("http://127.0.0.1:80/")[0] is False  # 80 not in allow_ports

    def test_unresolvable_host_is_refused_when_private_addresses_are_banned(self):
        policy = NetworkPolicy(allow_hosts=("nonexistent.invalid",))
        allowed, reason = policy.check_url("https://nonexistent.invalid/")
        assert allowed is False and "dns resolution failed" in reason

    def test_from_env_and_describe(self):
        policy = NetworkPolicy.from_env(" a.example.com , *.b.example.com ,, ")
        assert policy.allow_hosts == ("a.example.com", "*.b.example.com")
        assert policy.configured is True
        described = policy.describe()
        assert described["allow_hosts"] == ["a.example.com", "*.b.example.com"]
        assert described["configured"] is True

    def test_empty_env_is_unconfigured(self):
        assert NetworkPolicy.from_env("").configured is False

    def test_env_opt_ins_for_schemes_and_private_networks(self, monkeypatch):
        monkeypatch.setenv("SAF_NETWORK_SCHEMES", "https,http")
        monkeypatch.setenv("SAF_NETWORK_ALLOW_PRIVATE", "true")
        policy = NetworkPolicy.from_env("127.0.0.1")
        assert policy.allow_schemes == ("https", "http")
        assert policy.allow_private is True
        monkeypatch.setenv("SAF_NETWORK_ALLOW_PRIVATE", "no")
        assert NetworkPolicy.from_env("127.0.0.1").allow_private is False

    def test_env_can_extend_ports(self, monkeypatch):
        monkeypatch.setenv("SAF_NETWORK_PORTS", "8443, 9443 ,not-a-port")
        policy = NetworkPolicy.from_env("internal.example")
        assert policy.allow_ports == (8443, 9443)
        monkeypatch.setenv("SAF_NETWORK_PORTS", "")
        assert NetworkPolicy.from_env("internal.example").allow_ports == (443, 80)


# --------------------------------------------------------------------------- runtime

def _run(runtime, request):
    return asyncio.run(runtime.execute(request))


class TestWebRuntime:
    def test_fetches_and_returns_hash_evidence(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/page"))
        assert result.ok is True
        assert "fetched 1/1" in result.summary
        entry = result.evidence[0]["results"][0]
        assert entry["status"] == 200
        assert entry["bytes"] == len(BODY)
        assert entry["sha256"] == hashlib.sha256(BODY).hexdigest()
        assert entry["truncated"] is False
        assert "sovereign research payload" in entry["excerpt"]
        assert result.artifacts == [server + "/page"]

    def test_unconfigured_policy_refuses_without_connecting(self):
        runtime = WebRuntime(NetworkPolicy())  # empty allowlist
        result = _run(runtime, _request("https://example.com/"))
        assert result.ok is False
        assert "network policy is empty" in result.summary
        assert result.evidence[0]["error"] == "network_policy_unconfigured"

    def test_disallowed_host_is_reported_per_url(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request("https://not-allowed.example.com/"))
        assert result.ok is False
        assert "policy:" in result.evidence[0]["results"][0]["error"]

    def test_context_can_forbid_network(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/page", network=False))
        assert result.ok is False
        assert result.evidence[0]["error"] == "network_unavailable"

    def test_byte_cap_truncates_instead_of_buffering(self, server):
        runtime = WebRuntime(_policy(server, max_bytes=1024))
        result = _run(runtime, _request(server + "/big"))
        entry = result.evidence[0]["results"][0]
        assert entry["bytes"] == 1024
        assert entry["truncated"] is True
        assert entry["sha256"] == hashlib.sha256(b"B" * 1024).hexdigest()

    def test_http_error_is_reported_not_raised(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/fail"))
        assert result.ok is False
        entry = result.evidence[0]["results"][0]
        assert entry["status"] == 500 and "http 500" in entry["error"]

    def test_redirect_within_policy_is_followed(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/redirect"))
        entry = result.evidence[0]["results"][0]
        assert entry["ok"] is True
        assert entry["final_url"] == server + "/final"

    def test_redirect_out_of_policy_is_blocked(self, server):
        """A permitted host must not be able to bounce the fetch to a forbidden one."""
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/escape"))
        entry = result.evidence[0]["results"][0]
        assert result.ok is False
        assert "redirect blocked" in entry["error"] or "http 302" in entry["error"]

    def test_json_content_type_is_decoded(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/json"))
        entry = result.evidence[0]["results"][0]
        assert entry["content_type"].startswith("application/json")
        assert '"value": 42' in entry["excerpt"]

    def test_multiple_urls_and_limits(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request("", urls=[server + "/page", server + "/json"]))
        assert result.ok is True and "fetched 2/2" in result.summary
        too_many = _run(runtime, _request("", urls=[f"{server}/page?i={i}"
                                                   for i in range(MAX_URLS_PER_STEP + 1)]))
        assert too_many.ok is False and "too many urls" in too_many.summary

    def test_missing_urls_is_an_honest_error(self):
        result = _run(WebRuntime(NetworkPolicy(allow_hosts=("example.com",))),
                      _request("", urls=[]))
        assert result.ok is False
        assert "not a search engine" in result.summary

    def test_excerpt_is_bounded(self, server):
        runtime = WebRuntime(_policy(server))
        result = _run(runtime, _request(server + "/page"))
        assert len(result.evidence[0]["results"][0]["excerpt"]) <= EXCERPT_CHARS


def test_capability_is_registered_and_compiler_can_target_it():
    from saf.core.compiler import compile_intent
    from saf.runtime.bootstrap import build_registry

    assert "saf://web" in {r.resource_id for r in build_registry().all()}
    task = compile_intent("research https://example.com/report")
    assert task.required_capabilities == ["cap://research/web"]
