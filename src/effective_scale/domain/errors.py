"""Domain errors — typed, no stack-trace noise for expected failures."""
from __future__ import annotations


class DomainError(Exception):
    """Base class: a failure caused by client input or business rules."""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class StateError(DomainError):
    """Illegal state transition — an invariant violation surfaced safely."""

    code = "illegal_state_transition"
    http_status = 409


class CapacityError(DomainError):
    code = "capacity_exhausted"
    http_status = 507


class RateLimited(DomainError):
    code = "rate_limited"
    http_status = 429


class Backpressure(DomainError):
    code = "backpressure"
    http_status = 429


class Unauthorized(DomainError):
    code = "unauthorized"
    http_status = 401


class Forbidden(DomainError):
    code = "forbidden"
    http_status = 403


class InternalError(DomainError):
    code = "internal_error"
    http_status = 500
