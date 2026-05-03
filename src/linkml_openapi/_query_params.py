"""Shared query-param helpers.

Both the OpenAPI generator (`linkml_openapi.generator`) and the Spring
emitter (`linkml_openapi.spring.generator`) feed schemas through
``walk_query_params`` to compute the query-param surface for a given
class. The helper does all parsing, capability inference, auto-detection,
and validation; renderers consume the resulting QueryParamSurface and
turn it into Parameter objects (OpenAPI) or @RequestParam dicts (Spring).

Validation rules raise/warn here so both renderers inherit identical
messages.
"""
from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass

from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView

QUERY_PARAM_TOKENS: frozenset[str] = frozenset({"equality", "comparable", "sortable"})

# Slot ranges over which `comparable` (>=, <=, >, <) is well-defined.
COMPARABLE_RANGES: frozenset[str] = frozenset(
    {"integer", "float", "double", "decimal", "date", "datetime"}
)


@dataclass(frozen=True)
class QueryParamSpec:
    """One slot's contribution to the query-param surface."""
    slot: SlotDefinition
    capabilities: frozenset[str]   # subset of QUERY_PARAM_TOKENS


@dataclass(frozen=True)
class QueryParamSurface:
    """Result of walking a class for query params."""
    params: list[QueryParamSpec]
    sort_tokens: list[str]   # ["name", "-name", ...] or []


def _parse_csv(raw: str) -> list[str]:
    return [tok.strip().lower() for tok in raw.split(",") if tok.strip()]


def _capabilities_from_raw(
    raw: str | None, cls_name: str, slot_name: str
) -> frozenset[str] | None:
    """Parse `openapi.query_param` into a capability set.

    Returns None when absent or explicitly `false`. Unknown tokens are
    warned about and silently dropped.
    """
    if raw is None:
        return None
    tokens = set(_parse_csv(raw))
    if not tokens or tokens == {"false"}:
        return None
    unknown = tokens - {"true", "false"} - QUERY_PARAM_TOKENS
    if unknown:
        warnings.warn(
            f"Slot {cls_name}.{slot_name!r} declares unknown "
            f"openapi.query_param token(s) {sorted(unknown)!r}; "
            f"expected one or more of {sorted(QUERY_PARAM_TOKENS)!r}. "
            "Token(s) ignored - fix the typo or remove them.",
            stacklevel=4,
        )
    if "true" in tokens:
        tokens.add("equality")
        tokens.discard("true")
    if "comparable" in tokens or "sortable" in tokens:
        tokens.add("equality")
    valid = tokens & QUERY_PARAM_TOKENS
    return frozenset(valid) if valid else None


def walk_query_params(
    sv: SchemaView,
    cls: ClassDefinition,
    *,
    schema_auto_default: bool,
    is_slot_excluded: Callable[[SlotDefinition], bool],
    induced_slots: Callable[[str], list[SlotDefinition]],
    get_slot_annotation: Callable[[ClassDefinition, str, str], str | None],
    get_class_annotation: Callable[[ClassDefinition, str], str | None],
) -> QueryParamSurface:
    """Compute the query-param surface for `cls`.

    Behavior matches the existing OpenAPI generator:

    * If any slot has `openapi.query_param` (with a non-`false` value),
      auto-inference is suppressed for the rest of the class.
    * Otherwise, `openapi.auto_query_params` (class wins over schema)
      gates auto-inference. When enabled, scalar non-multivalued
      non-identifier slots get `equality`.
    * `comparable` and `sortable` imply `equality`.
    * Unknown tokens warn; `sortable` on multivalued raises;
      `comparable` on a non-ordered range warns.
    """
    auto_class = get_class_annotation(cls, "openapi.auto_query_params")
    if auto_class is not None:
        auto_enabled = auto_class.strip().lower() in ("true", "1", "yes")
    else:
        auto_enabled = schema_auto_default

    annotated: list[QueryParamSpec] = []
    inferred: list[QueryParamSpec] = []
    sort_tokens: list[str] = []
    has_explicit_param = False

    for slot in induced_slots(cls.name):
        if is_slot_excluded(slot):
            continue
        raw = get_slot_annotation(cls, slot.name, "openapi.query_param")
        caps = _capabilities_from_raw(raw, cls.name, slot.name)

        if caps is not None:
            has_explicit_param = True
            if "comparable" in caps:
                range_name = slot.range or "string"
                if range_name not in COMPARABLE_RANGES:
                    warnings.warn(
                        f"Slot {cls.name}.{slot.name!r} marked `comparable` but "
                        f"range {range_name!r} is not a numeric or temporal type; "
                        "comparison operators may behave unexpectedly.",
                        stacklevel=3,
                    )
            if "sortable" in caps:
                if slot.multivalued:
                    raise ValueError(
                        f"Slot {cls.name}.{slot.name!r} is multivalued; sort "
                        "order over a set is not well-defined. Remove `sortable` "
                        "or change the slot to single-valued."
                    )
                sort_tokens.extend([slot.name, f"-{slot.name}"])
            annotated.append(QueryParamSpec(slot=slot, capabilities=caps))
            continue

        # Slot-level explicit "false" - `_capabilities_from_raw` returned
        # None, but we still want to suppress auto-inference for this slot.
        if raw is not None and set(_parse_csv(raw)) == {"false"}:
            continue

        if not auto_enabled:
            continue
        if slot.multivalued or slot.identifier:
            continue
        range_name = slot.range or "string"
        is_scalar = range_name in ("string", "integer", "boolean") or (
            sv.get_enum(range_name) is not None
        )
        if is_scalar:
            inferred.append(
                QueryParamSpec(slot=slot, capabilities=frozenset({"equality"}))
            )

    # Annotated slots win when present; otherwise fall back to inferred.
    if has_explicit_param:
        return QueryParamSurface(params=annotated, sort_tokens=sort_tokens)
    return QueryParamSurface(params=inferred, sort_tokens=[])
