"""Hoist inline ``oneOf`` schemas in path operations to named components.

openapi-generator and similar codegens treat inline ``oneOf`` schemas
in path responses or request bodies as anonymous types. They
auto-name them (``ListDatasets200ResponseInner``), and several
templates flatten the union into a single DTO that carries every
branch's properties — losing the discriminator dispatch and
per-branch pinning.

Hoisting each unique inline ``oneOf`` to a named entry under
``components/schemas`` and replacing the inline shape with a
``$ref`` makes the polymorphic structure persist across the
LinkML → OpenAPI → openapi-generator → Spring → springdoc round
trip with the discriminator intact.

Naming: derived from the first ``$ref`` branch's class name plus a
``Variant`` suffix (e.g. ``DatasetVariant`` for an inline ``oneOf``
over Dataset/Catalog/DatasetSeries). Identical inline shapes
deduplicate to a single component; near-duplicates are
disambiguated with a numeric tail. The empty-string fallback
``PolymorphicVariant`` covers degenerate cases where no branches
have ``$ref``s.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


def extract_inline_oneof(spec: dict) -> dict:
    """Rewrite ``spec`` so every inline ``oneOf`` in a path operation
    becomes ``$ref`` to a named component schema.

    Mutates and returns ``spec``. Idempotent — running twice is a
    no-op because the second pass finds no remaining inline oneOfs.
    """
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})

    # Maps canonical-content-key → component name, so the same inline
    # oneOf appearing in N paths hoists to a single named component.
    seen: dict[str, str] = {}

    def _hoist(node: Any) -> Any:
        if isinstance(node, dict):
            if _is_inline_polymorphic_schema(node):
                return _hoist_one(node, seen, schemas)
            return {k: _hoist(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_hoist(item) for item in node]
        return node

    paths = spec.get("paths") or {}
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method_key in (
            "get", "put", "post", "delete", "patch", "options", "head", "trace"
        ):
            op = path_item.get(method_key)
            if not isinstance(op, dict):
                continue
            _walk_op(op, _hoist)

    return spec


def _is_inline_polymorphic_schema(node: dict) -> bool:
    """An inline ``oneOf`` schema worth hoisting.

    We only hoist polymorphic unions — those carrying a discriminator —
    since the codegen pain is around discriminator dispatch. A bare
    ``oneOf`` without a discriminator is structurally simpler and most
    codegens handle the inline form fine.
    """
    return "oneOf" in node and "discriminator" in node and "$ref" not in node


def _walk_op(op: dict, hoist) -> None:
    """Apply ``hoist`` to every inline schema reachable inside an
    operation: request body content schemas and response content
    schemas. Other operation fields (parameters, security, etc.) keep
    their inline schemas — those rarely use polymorphic ``oneOf``."""
    request_body = op.get("requestBody")
    if isinstance(request_body, dict):
        _hoist_content(request_body.get("content"), hoist)
    responses = op.get("responses") or {}
    for response in responses.values():
        if isinstance(response, dict):
            _hoist_content(response.get("content"), hoist)


def _hoist_content(content: Any, hoist) -> None:
    if not isinstance(content, dict):
        return
    for media in content.values():
        if not isinstance(media, dict):
            continue
        if "schema" in media:
            media["schema"] = hoist(media["schema"])


def _hoist_one(node: dict, seen: dict[str, str], schemas: dict) -> dict:
    """Replace ``node`` with a ``$ref`` and register the named component.

    The original ``node`` is what gets stored as the component (deep-
    copied so subsequent walks of the same path don't double-process
    it). The caller receives a fresh ``$ref`` dict.
    """
    key = json.dumps(node, sort_keys=True)
    if key in seen:
        return {"$ref": f"#/components/schemas/{seen[key]}"}

    name = _derive_name(node, schemas)
    schemas[name] = deepcopy(node)
    seen[key] = name
    return {"$ref": f"#/components/schemas/{name}"}


def _derive_name(node: dict, schemas: dict) -> str:
    """Pick a stable, descriptive component name for an inline oneOf.

    The first ``$ref`` branch usually points at the polymorphic root
    (the most general type in the union), so its class name is the
    natural seed: ``Dataset`` + ``Variant`` → ``DatasetVariant``.
    Falls back to ``PolymorphicVariant`` when no branches are
    ``$ref``s.
    """
    seed = "Polymorphic"
    for branch in node.get("oneOf") or []:
        if isinstance(branch, dict) and "$ref" in branch:
            ref = branch["$ref"]
            seed = ref.rsplit("/", 1)[-1]
            break
    base = f"{seed}Variant"
    candidate = base
    counter = 2
    while candidate in schemas:
        candidate = f"{base}{counter}"
        counter += 1
    return candidate
