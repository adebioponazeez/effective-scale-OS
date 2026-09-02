"""Id generation, hashing and constant-time comparison helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid


def new_id() -> str:
    return uuid.uuid4().hex


def new_nonce() -> str:
    return secrets.token_hex(16)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def event_uuid() -> str:
    return str(uuid.uuid4())
