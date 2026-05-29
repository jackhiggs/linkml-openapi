"""Shared utilities for the OpenAPI generator and the Spring emitter.

Lives in its own module so the Spring side can import without
reaching into ``linkml_openapi.generator`` for private names —
mirrors the pattern established by ``_chains.py`` / ``_query_params.py``
/ ``_http.py``.

Membership rule for this module: a helper belongs here when it has
no dependency on the LinkML SchemaView, is needed by both emitters,
and is small enough that copy-paste would be tempting otherwise.
"""

from __future__ import annotations

import re

# Class-name suffixes that are already plural (or unchanged in plural form)
# and should be returned as-is from `pluralize`.
_INVARIANT_PLURAL_SUFFIXES = ("series", "species", "genus")

# Class-name suffixes that become irregular in plural form. We don't try
# to inflect these — we just emit a heads-up so the user can set
# `openapi.path` explicitly. Listed lower-case for case-insensitive match.
_IRREGULAR_HINT_SUFFIXES = (
    "child",
    "datum",
    "criterion",
    "phenomenon",
    "analysis",
    "thesis",
    "axis",
    "crisis",
)


def pluralize(name: str) -> str:
    """Pluralize an English noun for URL paths.

    Handles the common regular-pluralization patterns (`-s/-x/-z/-ch/-sh`,
    consonant-`y`, default `+s`) and the most common already-plural Latin
    forms used in domain modeling (`series`, `species`, `genus`). For
    irregular nouns (`child`, `person`, `index`, …) the function falls
    back to `+s` and the caller is expected to set `openapi.path`
    explicitly when correctness matters.
    """
    if not name:
        return name

    lower = name.lower()
    for inv in _INVARIANT_PLURAL_SUFFIXES:
        if lower.endswith(inv):
            return name

    if name.endswith(("ch", "sh")):
        return name + "es"
    if name.endswith(("s", "x", "z")):
        return name + "es"
    if name.endswith("y") and name[-2:] not in ("ay", "ey", "oy", "uy"):
        return name[:-1] + "ies"
    return name + "s"


def is_irregular_plural_hint(name: str) -> bool:
    """True when `name` looks like it would be misled by ``pluralize``'s
    default rules. Used by the OpenAPI generator to surface a warning
    at generation time so the user can set ``openapi.path`` explicitly.
    """
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(suf) for suf in _IRREGULAR_HINT_SUFFIXES)


def to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return s.lower()


def to_path_segment(name: str) -> str:
    """Convert class name to URL path segment: CamelCase → snake_case → plural."""
    return pluralize(to_snake_case(name))


def is_truthy(value: object | None) -> bool:
    """Check if an annotation value represents a boolean true.

    ``None`` (annotation absent) is not truthy. Use this for opt-in
    annotations (``openapi.resource: "true"``, etc.) so the parsing
    rule is identical across both generators.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def is_falsy(value: object | None) -> bool:
    """Check if an annotation value represents a boolean false.

    ``None`` (annotation absent) is NOT falsy — distinguish "the
    author explicitly opted out" from "the author didn't say anything."
    Use this for opt-out annotations (``openapi.expose: "false"``,
    ``openapi.codegen_inheritance: "false"``, etc.) so the falsy
    parsing rule is identical across both generators.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    return str(value).strip().lower() == "false"


def parse_csv(value: str | None, *, lowercase: bool = False) -> list[str]:
    """Split a comma-separated annotation value, trimming whitespace
    and empties. ``lowercase=True`` normalises every token (used for
    case-insensitive comparisons of HTTP methods, operation kinds,
    etc.)."""
    if not value:
        return []
    out = [t.strip() for t in str(value).split(",")]
    if lowercase:
        out = [t.lower() for t in out]
    return [t for t in out if t]
