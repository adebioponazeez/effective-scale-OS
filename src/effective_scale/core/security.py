"""Token auth: HMAC-signed capability tokens, offline-verifiable, scoped.

Format: `es1.<payload_b64>.<hmac_hex>`
  payload = {"v":1,"ns":<namespace>,"sc":[...],"iat":<ts>,"exp":<ts>,"jti":<id>}
The raw token is hashed at rest (rows carry only sha256(raw)), so a store dump
cannot be replayed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from ..domain.errors import Unauthorized, Forbidden
from ..domain.ids import hash_token


@dataclass(frozen=True)
class Claims:
    token_id: str
    namespace: str
    scopes: frozenset[str]
    expires_at: float | None


class TokenService:
    def __init__(self, secret: str, *, ttl: float = 86400 * 30, now=None):
        if not secret or len(secret) < 16:
            raise ValueError("auth secret must be >= 16 characters")
        self._secret = secret.encode("utf-8")
        self._ttl = ttl
        self._now = now or time.time

    # -- issuance ------------------------------------------------------------
    def issue(self, token_id: str, namespace: str, scopes: list[str], ttl: float | None = None) -> str:
        lifetime = self._ttl if ttl is None else ttl
        payload = {
            "v": 1,
            "ns": namespace,
            "sc": scopes,
            "iat": int(self._now()),
            "exp": int(self._now() + lifetime),
            "jti": token_id,
        }
        body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"es1.{body}.{sig}"

    # -- verification ----------------------------------------------------------
    def verify(self, raw: str | None) -> Claims | None:
        if not raw:
            return None
        parts = raw.strip().split(".")
        if len(parts) != 3 or parts[0] != "es1":
            return None
        body, sig = parts[1], parts[2]
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception:  # noqa: BLE001 — malformed token is simply invalid
            return None
        if payload.get("v") != 1:
            return None
        exp = payload.get("exp")
        if exp is not None and int(time.time()) > int(exp):
            return None
        return Claims(
            token_id=payload.get("jti", ""),
            namespace=payload.get("ns", ""),
            scopes=frozenset(payload.get("sc", [])),
            expires_at=float(exp) if exp else None,
        )

    def require(self, raw: str | None, *, namespace: str | None = None, scope: str = "read") -> Claims:
        claims = self.verify(raw)
        if claims is None:
            raise Unauthorized("invalid or expired token")
        if namespace is not None and claims.namespace != namespace:
            raise Forbidden("token is not scoped to this namespace")
        if scope != "read" and scope not in claims.scopes:
            raise Forbidden(f"token missing scope '{scope}'")
        return claims

    @staticmethod
    def store_hash(raw: str) -> str:
        return hash_token(raw)
