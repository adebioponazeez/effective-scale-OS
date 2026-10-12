"""Network policy — the boundary that makes outbound capabilities safe to ship.

Everything that leaves the process (currently the research runtime, later the model
providers) must pass through `NetworkPolicy.check_url` first. The policy is **deny by
default**: with no configured hosts, nothing is reachable, so an unconfigured deployment
cannot exfiltrate or be used as an SSRF pivot.

Controls, in order:

1. **Scheme allowlist** — `https` only by default; `http` must be opted into explicitly.
2. **Host allowlist** — exact hosts, or `*.suffix` for subdomains only (never the bare
   domain, so `*.example.com` cannot be tricked into allowing `example.com` itself unless
   it is listed too).
3. **SSRF guard** — the host is resolved and *every* returned address is checked; private,
   loopback, link-local, reserved, multicast and unspecified addresses are refused unless
   `allow_private` is set (tests and deliberate on-prem deployments).
4. **Ports** — `443`/`80` by default, extendable explicitly.
5. **Size and time** — `max_bytes` and `timeout_s` bound what a single request can cost.

The policy is deliberately a *value object*: it is constructed at composition time, carried
by the runtime, and never mutated per request.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_SCHEMES = ("https",)
DEFAULT_PORTS = (443, 80)
DENIED_IP_FLAGS = ("is_private", "is_loopback", "is_link_local", "is_reserved",
                   "is_multicast", "is_unspecified")


@dataclass(frozen=True)
class NetworkPolicy:
    """A deny-by-default allowlist for outbound HTTP(S)."""

    allow_hosts: tuple[str, ...] = ()
    allow_schemes: tuple[str, ...] = DEFAULT_SCHEMES
    allow_ports: tuple[int, ...] = DEFAULT_PORTS
    allow_private: bool = False
    max_bytes: int = 2_000_000
    timeout_s: float = 15.0
    max_redirects: int = 3
    _denied_cache: dict = field(default_factory=dict, compare=False, repr=False)

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_env(cls, raw: str | None = None, **overrides) -> "NetworkPolicy":
        """Build from the environment.

        `SAF_NETWORK_ALLOW`      comma-separated hosts (empty = deny everything)
        `SAF_NETWORK_SCHEMES`    comma-separated schemes (default https; add http explicitly)
        `SAF_NETWORK_PORTS`      comma-separated ports (default 443/80; on-prem services use more)
        `SAF_NETWORK_ALLOW_PRIVATE`  truthy to permit private/loopback targets (on-prem only)
        """
        import os

        def truthy(value: str | None) -> bool:
            return str(value or "").strip().lower() in ("1", "true", "yes", "on")

        value = raw if raw is not None else os.getenv("SAF_NETWORK_ALLOW", "")
        hosts = tuple(part.strip().lower() for part in value.split(",") if part.strip())
        values = dict(allow_hosts=hosts)
        schemes = os.getenv("SAF_NETWORK_SCHEMES", "")
        if schemes:
            values["allow_schemes"] = tuple(p.strip().lower() for p in schemes.split(",") if p.strip())
        ports = os.getenv("SAF_NETWORK_PORTS", "")
        if ports:
            parsed = tuple(int(p) for p in ports.split(",") if p.strip().isdigit())
            if parsed:
                values["allow_ports"] = parsed
        if truthy(os.getenv("SAF_NETWORK_ALLOW_PRIVATE")):
            values["allow_private"] = True
        values.update(overrides)
        return cls(**values)

    @property
    def configured(self) -> bool:
        return bool(self.allow_hosts)

    # ------------------------------------------------------------------ decisions

    def check_url(self, url: str) -> tuple[bool, str]:
        """Return (allowed, reason). `reason` is safe to show an operator."""
        try:
            parts = urlsplit(str(url).strip())
        except ValueError:
            return False, "unparseable url"
        scheme = (parts.scheme or "").lower()
        if scheme not in self.allow_schemes:
            return False, f"scheme '{scheme or '<none>'}' not allowed (allowed: {list(self.allow_schemes)})"
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return False, "url has no host"
        if not self._host_allowed(host):
            return False, (f"host '{host}' is not in the allowlist"
                           + ("" if self.configured else " (it is empty: deny by default)"))
        port = parts.port or (443 if scheme == "https" else 80)
        if port not in self.allow_ports:
            return False, f"port {port} not allowed (allowed: {list(self.allow_ports)})"
        if not self.allow_private:
            ok, reason = self._addresses_public(host, port)
            if not ok:
                return False, reason
        return True, "allowed"

    def host_permitted(self, host: str) -> bool:
        """Static allowlist check: does the list permit this host, ignoring DNS?

        `check_url` is the security decision (it also resolves and validates addresses);
        this is the rule lookup, exposed so operators and tests can reason about the list
        without network access.
        """
        return self._host_allowed(str(host).lower().rstrip("."))

    def _host_allowed(self, host: str) -> bool:
        for entry in self.allow_hosts:
            if entry.startswith("*."):
                suffix = entry[2:]
                if host.endswith("." + suffix):  # subdomains only, not the bare domain
                    return True
            elif host == entry:
                return True
        return False

    def _addresses_public(self, host: str, port: int) -> tuple[bool, str]:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            return False, f"dns resolution failed for '{host}': {exc.strerror or exc}"
        addresses = {info[4][0] for info in infos}
        for address in sorted(addresses):
            try:
                ip = ipaddress.ip_address(address.split("%")[0])
            except ValueError:
                return False, f"unparseable address for '{host}': {address}"
            for flag in DENIED_IP_FLAGS:
                if getattr(ip, flag, False):
                    return False, (f"host '{host}' resolves to a non-public address "
                                   f"({address}); set allow_private for on-prem targets")
        return True, "public"

    # ------------------------------------------------------------------ introspection

    def describe(self) -> dict:
        """Operator-facing view: what this policy would permit, without resolving anything."""
        return {
            "configured": self.configured,
            "allow_hosts": list(self.allow_hosts),
            "allow_schemes": list(self.allow_schemes),
            "allow_ports": list(self.allow_ports),
            "allow_private": self.allow_private,
            "max_bytes": self.max_bytes,
            "timeout_s": self.timeout_s,
        }
