"""Shared HTTP-status reason phrases for the OpenAPI generator and
the Spring emitter.

Lives in its own module so a future edit to the table can't silently
drift between the two emitters — both import the same constant.
Sourced from IANA's HTTP Status Code Registry; only the 4xx and 5xx
codes a schema author would plausibly declare via
``openapi.error_responses`` are included. Codes outside the table
fall back to ``f"HTTP {code}"`` at the call site.
"""

from __future__ import annotations

HTTP_REASON_PHRASES: dict[int, str] = {
    400: "Bad request",
    401: "Unauthorized",
    402: "Payment required",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    406: "Not acceptable",
    408: "Request timeout",
    409: "Conflict",
    410: "Gone",
    412: "Precondition failed",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Validation error",
    423: "Locked",
    424: "Failed dependency",
    428: "Precondition required",
    429: "Too many requests",
    451: "Unavailable for legal reasons",
    500: "Server error",
    501: "Not implemented",
    502: "Bad gateway",
    503: "Service unavailable",
    504: "Gateway timeout",
}


def reason_for(code: int) -> str:
    """Return the standard reason phrase for an HTTP status code, or
    a generic fallback when the code isn't in the table."""
    return HTTP_REASON_PHRASES.get(code, f"HTTP {code}")
