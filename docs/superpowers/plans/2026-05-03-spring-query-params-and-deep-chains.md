# Spring emitter: query params and deep nested chains — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the LinkML → Spring emitter to parity with the OpenAPI generator on slot-driven query parameters and deep-nested URL chains, closing the contract drift between the sidecar OpenAPI spec and the generated controllers.

**Architecture:** Extract two pure-helper modules (`_query_params.py`, `_chains.py`) shared by both generators. The OpenAPI generator is refactored into thin renderers around the helpers (no behavior change). The Spring generator gets new `_query_param_dicts`, `_deep_chained_ops`, and `_deep_templated_ops` methods that consume the same helpers and render Java.

**Tech Stack:** Python 3.11+, `linkml-runtime` (SchemaView), `jinja2` (templates), `pytest` (tests). The Spring side targets Java 17 / Spring Boot 3 / springdoc.

**Spec:** `docs/superpowers/specs/2026-05-03-spring-query-params-and-deep-chains-design.md`

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `src/linkml_openapi/_query_params.py` | NEW | Shared query-param helpers: `walk_query_params()`, `QueryParamSpec`, `QueryParamSurface`, capability/range constants. Pure — no rendering decisions. |
| `src/linkml_openapi/_chains.py` | NEW | Shared chain helpers: `build_parent_chains_index()`, `canonical_parent_chain()`, `parse_path_param_sources()`, `render_chain_hops()`, `ChainHop` dataclass. |
| `src/linkml_openapi/generator.py` | MODIFY | `_make_query_params`, `_collect_parent_chains`, `_canonical_parent_chain`, `_emit_chained_deep_path`, `_emit_templated_deep_path`, `_parse_path_param_sources`, `_query_param_capabilities`, `_auto_query_params_enabled` shrink to thin wrappers around the new helpers. Behavior unchanged. |
| `src/linkml_openapi/spring/generator.py` | MODIFY | Add `_query_param_dicts()`, `_render_query_param_spec()`, `_render_sort_param()`, `_deep_chained_ops()`, `_deep_templated_ops()`, `_chains_index` cache. Wire query params into `_top_level_ops` / `_composition_ops` / `_reference_ops` list operations. Wire deep emission into `_render_api` with `nested_only` / `flat_only` gating. |
| `tests/test_query_params_helper.py` | NEW | Unit tests for `_query_params.walk_query_params()`. |
| `tests/test_chains_helper.py` | NEW | Unit tests for `_chains.*`. |
| `tests/test_linkml_spring.py` | MODIFY | Add `TestQueryParams`, `TestDeepChainedPaths`, `TestTemplatedPaths`, `TestParityWithOpenApiSide`. |
| `tests/fixtures/spring_query_params.yaml` | NEW | Tiny fixture covering equality / comparable / sortable / `false` / multivalued. |
| `tests/fixtures/spring_path_template.yaml` | NEW | Tiny fixture for templated paths. |
| `tests/fixtures/spring_nested_only.yaml` | NEW | Tiny fixture for `nested_only` and `flat_only` gating + ambiguous chains. |

---

## Phase 1 — Extract `_query_params.py`

Pure refactor. The OpenAPI generator's existing tests (no behavior change expected) must remain green.

### Task 1.1: Create the shared query-params helper module

**Files:**
- Create: `src/linkml_openapi/_query_params.py`
- Create: `tests/test_query_params_helper.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_query_params_helper.py
"""Unit tests for the shared query-param helper.

Both the OpenAPI generator and the Spring emitter feed schemas through
walk_query_params(); the helper does all parsing, capability inference,
auto-detection, and validation. These tests pin the contract independent
of either renderer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from linkml_runtime.utils.schemaview import SchemaView

from linkml_openapi._query_params import (
    walk_query_params,
    QueryParamSpec,
    QueryParamSurface,
)


def _sv_from_text(yaml: str, tmp_path: Path) -> SchemaView:
    p = tmp_path / "schema.yaml"
    p.write_text(yaml)
    return SchemaView(str(p))


SCHEMA_BASE = """\
id: https://example.org/qp
name: qp
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
classes:
  Person:
    annotations:
      openapi.resource: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
      name: { range: string }
      age: { range: integer }
      email: { range: string }
      tags: { range: string, multivalued: true }
"""


def _walk(sv: SchemaView, class_name: str, schema_auto_default: bool = True):
    cls = sv.get_class(class_name)
    return walk_query_params(
        sv,
        cls,
        schema_auto_default=schema_auto_default,
        is_slot_excluded=lambda s: False,
        induced_slots=lambda name: list(sv.class_induced_slots(name)),
        get_slot_annotation=_get_slot_ann,
        get_class_annotation=_get_class_ann,
    )


def _get_slot_ann(cls, slot_name, tag):
    if not cls.slot_usage:
        return None
    su = cls.slot_usage.get(slot_name) if isinstance(cls.slot_usage, dict) else None
    if su is None:
        return None
    anns = getattr(su, "annotations", None)
    if not anns:
        return None
    for ann in anns.values() if isinstance(anns, dict) else [anns]:
        if getattr(ann, "tag", None) == tag:
            return str(ann.value)
    return None


def _get_class_ann(cls, tag):
    if not cls or not cls.annotations:
        return None
    for ann in cls.annotations.values():
        if ann.tag == tag:
            return str(ann.value)
    return None


def test_auto_inference_picks_scalar_slots(tmp_path):
    sv = _sv_from_text(SCHEMA_BASE, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert "name" in names
    assert "age" in names
    assert "email" in names
    assert "tags" not in names    # multivalued auto-excluded
    assert "id" not in names      # identifier auto-excluded
    for spec in surface.params:
        assert spec.capabilities == frozenset({"equality"})
    assert surface.sort_tokens == []


def test_explicit_equality_token(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: equality
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    assert name_spec.capabilities == frozenset({"equality"})


def test_comparable_implies_equality(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      age:
        annotations:
          openapi.query_param: comparable
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    age_spec = next(s for s in surface.params if s.slot.name == "age")
    assert age_spec.capabilities == frozenset({"equality", "comparable"})


def test_sortable_implies_equality_and_emits_sort_tokens(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: sortable
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    assert name_spec.capabilities == frozenset({"equality", "sortable"})
    assert surface.sort_tokens == ["name", "-name"]


def test_query_param_false_excludes_slot_from_auto(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      email:
        annotations:
          openapi.query_param: "false"
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert "email" not in names


def test_auto_query_params_false_at_schema_level_disables_inference(tmp_path):
    sv = _sv_from_text(SCHEMA_BASE, tmp_path)
    surface = _walk(sv, "Person", schema_auto_default=False)
    assert surface.params == []


def test_auto_query_params_class_level_overrides_schema(tmp_path):
    yaml = SCHEMA_BASE.replace(
        '      openapi.resource: "true"',
        '      openapi.resource: "true"\n      openapi.auto_query_params: "true"',
    )
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person", schema_auto_default=False)
    names = [spec.slot.name for spec in surface.params]
    assert "name" in names


def test_unknown_token_warns_and_ignores(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: sorteable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.warns(UserWarning, match="sorteable"):
        surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    # auto-inference still applies (typo'd token treated as no annotation)
    assert name_spec.capabilities == frozenset({"equality"})


def test_sortable_on_multivalued_raises(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      tags:
        annotations:
          openapi.query_param: sortable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.raises(ValueError, match="multivalued"):
        _walk(sv, "Person")


def test_comparable_on_string_warns(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: comparable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.warns(UserWarning, match="not a numeric or temporal"):
        _walk(sv, "Person")


def test_annotated_class_disables_auto_inference_for_unannotated_slots(tmp_path):
    """When ANY slot on the class has openapi.query_param, auto-inference
    is suppressed for the rest. Matches existing OpenAPI generator behavior."""
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: equality
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert names == ["name"]   # age, email NOT auto-inferred
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/jackhiggs/workspace/linkml-openapi
pytest tests/test_query_params_helper.py -v
```

Expected: every test fails with `ModuleNotFoundError: No module named 'linkml_openapi._query_params'`.

- [ ] **Step 3: Create the helper module**

```python
# src/linkml_openapi/_query_params.py
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
            "Token(s) ignored — fix the typo or remove them.",
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

        # Slot-level explicit "false" — `_capabilities_from_raw` returned
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_query_params_helper.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/linkml_openapi/_query_params.py tests/test_query_params_helper.py
git commit -m "$(cat <<'EOF'
feat(_query_params): extract shared helper for query-param walking

Pure helper consumed by both the OpenAPI generator and the Spring
emitter. Owns capability-token parsing, auto-inference, opt-out,
unknown-token warnings, comparable-range warnings, and the
sortable-on-multivalued ValueError.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2: Refactor OpenAPI generator to use the helper

**Files:**
- Modify: `src/linkml_openapi/generator.py:2882-3079` (`_make_query_params`, `_query_param_capabilities`, `_auto_query_params_enabled`, `_add_annotated_query_params`, plus the `_QUERY_PARAM_TOKENS` / `_COMPARABLE_RANGES` class constants)

- [ ] **Step 1: Replace the constants with imports**

In `src/linkml_openapi/generator.py`, near the top of the file (with the other imports):

```python
from linkml_openapi._query_params import (
    walk_query_params,
    QUERY_PARAM_TOKENS as _QUERY_PARAM_TOKENS,
    COMPARABLE_RANGES as _COMPARABLE_RANGES,
)
```

Then delete the class-level `_COMPARABLE_RANGES` and `_QUERY_PARAM_TOKENS` definitions inside `OpenAPIGenerator` (lines 2885 and 2887).

- [ ] **Step 2: Replace `_make_query_params` body with a thin renderer**

Replace `_make_query_params` (lines 2944-3015) with:

```python
def _make_query_params(self, cls: ClassDefinition) -> list[Parameter]:
    """Generate query parameters for the list endpoint.

    Delegates capability parsing, auto-inference, and validation to
    `_query_params.walk_query_params`. This method only renders
    Parameter objects — wire shape stays unchanged.
    """
    params: list[Parameter] = [
        Parameter(
            name="limit",
            param_in=ParameterLocation.QUERY,
            param_schema=Schema(type=DataType.INTEGER, default=100),
        ),
        Parameter(
            name="offset",
            param_in=ParameterLocation.QUERY,
            param_schema=Schema(type=DataType.INTEGER, default=0),
        ),
    ]
    surface = walk_query_params(
        self.schemaview,
        cls,
        schema_auto_default=_is_truthy(
            self._schema_annotation("openapi.auto_query_params") or "true"
        ),
        is_slot_excluded=self._is_slot_excluded,
        induced_slots=lambda name: list(self._induced_slots_iter(name)),
        get_slot_annotation=self._get_slot_annotation,
        get_class_annotation=self._class_annotation,
    )
    for spec in surface.params:
        params.extend(self._render_query_param_for_spec(spec))
    if surface.sort_tokens:
        params.append(self._make_sort_param(surface.sort_tokens))
    return params


def _render_query_param_for_spec(self, spec: QueryParamSpec) -> list[Parameter]:
    """Render one QueryParamSpec into one or more Parameter objects.

    `equality` → single param. `comparable` → four `__gte`/`__lte`/
    `__gt`/`__lt` params. `sortable` doesn't produce a per-slot
    Parameter (it contributes to the shared `?sort=` array param built
    by _make_sort_param).
    """
    out: list[Parameter] = []
    if "equality" in spec.capabilities:
        out.append(
            Parameter(
                name=spec.slot.name,
                param_in=ParameterLocation.QUERY,
                required=False,
                param_schema=self._slot_to_schema(spec.slot),
            )
        )
    if "comparable" in spec.capabilities:
        for op in ("gte", "lte", "gt", "lt"):
            out.append(
                Parameter(
                    name=f"{spec.slot.name}__{op}",
                    param_in=ParameterLocation.QUERY,
                    required=False,
                    param_schema=self._slot_to_schema(spec.slot),
                )
            )
    return out
```

Add at the top of `generator.py` near the helper imports:

```python
from linkml_openapi._query_params import QueryParamSpec, walk_query_params
```

(combine with the import from Step 1 — single import line).

- [ ] **Step 3: Delete the now-unused methods**

Delete from `OpenAPIGenerator`:

- `_query_param_capabilities` (lines 2889-2926)
- `_auto_query_params_enabled` (lines 2928-2942)
- `_add_annotated_query_params` (lines 3017-3062)

Keep `_make_sort_param` (line 3065) — it's still called from `_make_query_params`.

- [ ] **Step 4: Run the OpenAPI test suite to confirm no regressions**

```bash
cd /Users/jackhiggs/workspace/linkml-openapi
pytest tests/test_generator.py tests/test_dcat3.py tests/test_post_processors.py -v
```

Expected: all existing tests pass. If any fail, the helper signature or rendering shape diverged from the original — fix before continuing.

- [ ] **Step 5: Commit**

```bash
git add src/linkml_openapi/generator.py
git commit -m "$(cat <<'EOF'
refactor(generator): use _query_params helper for query-param walking

Replaces _make_query_params, _query_param_capabilities,
_auto_query_params_enabled, and _add_annotated_query_params with a
thin renderer that delegates to walk_query_params(). Behavior unchanged
— full test suite still green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2 — Extract `_chains.py`

Same playbook: extract pure helpers, refactor the OpenAPI generator into thin renderers, confirm zero behavior change.

### Task 2.1: Create the shared chains helper

**Files:**
- Create: `src/linkml_openapi/_chains.py`
- Create: `tests/test_chains_helper.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chains_helper.py
"""Unit tests for the shared chains helper."""
from __future__ import annotations

from pathlib import Path

import pytest
from linkml_runtime.utils.schemaview import SchemaView

from linkml_openapi._chains import (
    build_parent_chains_index,
    canonical_parent_chain,
    parse_path_param_sources,
)


SCHEMA_LINEAR = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      datasets:
        range: Dataset
        multivalued: true
  Dataset:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      distributions:
        range: Distribution
        multivalued: true
  Distribution:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""


def _sv(yaml: str, tmp_path: Path) -> SchemaView:
    p = tmp_path / "schema.yaml"
    p.write_text(yaml)
    return SchemaView(str(p))


def _build_index(sv: SchemaView):
    resource_classes = {
        name for name in sv.all_classes()
        if (sv.get_class(name).annotations or {})
        and any(
            ann.tag == "openapi.resource" and str(ann.value) == "true"
            for ann in sv.get_class(name).annotations.values()
        )
    }
    return build_parent_chains_index(
        sv,
        resource_classes=resource_classes,
        excluded_classes=set(),
        is_slot_excluded=lambda s: False,
        get_slot_annotation=lambda cls, slot, tag: None,
        induced_slots=lambda name: list(sv.class_induced_slots(name)),
    )


def test_chain_index_for_linear_schema(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert index["Distribution"] == [
        [("Catalog", "datasets"), ("Dataset", "distributions")]
    ]
    assert index["Dataset"] == [[("Catalog", "datasets")]]
    assert "Catalog" not in index   # root, no parent chain


def test_canonical_chain_with_one_chain(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert canonical_parent_chain(
        "Distribution", index, parent_path_annotation=None
    ) == [("Catalog", "datasets"), ("Dataset", "distributions")]


def test_canonical_chain_no_parents_returns_empty(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert canonical_parent_chain("Catalog", index, None) == []


def test_canonical_chain_ambiguous_without_annotation_raises(tmp_path):
    yaml = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""
    sv = _sv(yaml, tmp_path)
    index = _build_index(sv)
    with pytest.raises(ValueError, match="multiple parent chains"):
        canonical_parent_chain("Tag", index, None)


def test_canonical_chain_picks_match_via_parent_path(tmp_path):
    yaml = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""
    sv = _sv(yaml, tmp_path)
    index = _build_index(sv)
    chain = canonical_parent_chain("Tag", index, "Folder.tags")
    assert chain == [("Folder", "tags")]


def test_parse_path_param_sources_basic():
    out = parse_path_param_sources(
        "ResourceVersion",
        "cId:Catalog.id,doi:ResourceVersion.doi,version:ResourceVersion.version",
    )
    assert out == {
        "cId": ("Catalog", "id"),
        "doi": ("ResourceVersion", "doi"),
        "version": ("ResourceVersion", "version"),
    }


def test_parse_path_param_sources_malformed_raises():
    with pytest.raises(ValueError, match="malformed"):
        parse_path_param_sources("X", "noclassdotpart:notvalidsource")
    with pytest.raises(ValueError, match="malformed"):
        parse_path_param_sources("X", "missingcolon")


def test_parse_path_param_sources_duplicate_name_raises():
    with pytest.raises(ValueError, match="duplicate"):
        parse_path_param_sources("X", "id:Foo.id,id:Bar.id")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_chains_helper.py -v
```

Expected: every test fails with `ModuleNotFoundError`.

- [ ] **Step 3: Create the helper module**

```python
# src/linkml_openapi/_chains.py
"""Shared helpers for parent-chain detection and path-template parsing.

Used by both the OpenAPI generator and the Spring emitter to compute the
deep-nested URL chain for a leaf class. Pure: SchemaView in, dataclasses
out. Path-style and `path_id` rendering stay in the calling generator
(injected via callbacks) because they need access to that generator's
already-implemented path-style state.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView


PATH_TEMPLATE_PLACEHOLDER_RE: ClassVar[re.Pattern[str]] = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class ChainHop:
    """One hop of a deep-nested URL chain."""
    parent_class: str
    parent_id_param_name: str        # honors openapi.path_id
    parent_path_segment: str          # honors openapi.path / path_style
    slot_segment: str                 # honors openapi.path_segment / path_style
    parent_id_slot: SlotDefinition    # for typing the path param


def _parse_csv(raw: str) -> list[str]:
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def build_parent_chains_index(
    sv: SchemaView,
    *,
    resource_classes: set[str],
    excluded_classes: set[str],
    is_slot_excluded: Callable[[SlotDefinition], bool],
    get_slot_annotation: Callable[..., str | None],
    induced_slots: Callable[[str], list[SlotDefinition]],
) -> dict[str, list[list[tuple[str, str]]]]:
    """Index every chain of `(parent_class, slot_name)` leading to each class.

    Result is keyed by leaf class name. Each value is a list of chains;
    each chain is a list of `(parent_class, slot_name)` tuples ordered
    root → direct parent. A class with no parents in the relationship
    graph is absent from the dict.

    Cycles are detected (the recursion tracks visited classes on the
    current path) so a graph with `A.bs: list[B]` and `B.as: list[A]`
    doesn't blow up.
    """
    direct_parents: dict[str, list[tuple[str, str]]] = {}
    for parent_name in sv.all_classes():
        if parent_name in excluded_classes:
            continue
        if parent_name not in resource_classes:
            continue
        parent_cls = sv.get_class(parent_name)
        for slot in induced_slots(parent_name):
            if not slot.multivalued:
                continue
            if is_slot_excluded(slot):
                continue
            target = slot.range
            if not target or sv.get_class(target) is None:
                continue
            if target == parent_name:
                continue   # self-loop
            nested_ann = get_slot_annotation(parent_cls, slot.name, "openapi.nested")
            if nested_ann is not None and nested_ann.strip().lower() not in (
                "true", "1", "yes"
            ):
                continue
            direct_parents.setdefault(target, []).append((parent_name, slot.name))

    index: dict[str, list[list[tuple[str, str]]]] = {}

    def walk(leaf: str, on_path: tuple[str, ...]) -> list[list[tuple[str, str]]]:
        chains: list[list[tuple[str, str]]] = []
        for parent_name, slot_name in direct_parents.get(leaf, []):
            if parent_name in on_path:
                continue
            upper = walk(parent_name, on_path + (parent_name,))
            if not upper:
                chains.append([(parent_name, slot_name)])
            else:
                for u in upper:
                    chains.append(u + [(parent_name, slot_name)])
        return chains

    for cls_name in list(direct_parents.keys()):
        chains = walk(cls_name, (cls_name,))
        if chains:
            index[cls_name] = chains
    return index


def parent_path_segments(annotation: str) -> list[tuple[str | None, str]]:
    """Parse an `openapi.parent_path` annotation into per-hop matchers.

    Each `/`-separated segment is either `slot_name` (parent class
    implied, must be unambiguous) or `ClassName.slot_name`
    (class-qualified). Returns `[(class_or_none, slot_name), ...]`.
    """
    segments: list[tuple[str | None, str]] = []
    for raw in annotation.strip().split("/"):
        raw = raw.strip()
        if not raw:
            continue
        if "." in raw:
            cls_name, slot_name = raw.split(".", 1)
            segments.append((cls_name.strip() or None, slot_name.strip()))
        else:
            segments.append((None, raw))
    return segments


def canonical_parent_chain(
    class_name: str,
    index: dict[str, list[list[tuple[str, str]]]],
    parent_path_annotation: str | None,
) -> list[tuple[str, str]]:
    """Pick the canonical chain for `class_name`.

    * 0 chains in index → returns `[]`.
    * 1 chain → returns it.
    * >1 chains → reads `openapi.parent_path` and matches.
    """
    chains = index.get(class_name, [])
    if not chains:
        return []
    if len(chains) == 1:
        return chains[0]
    candidates_qualified = [
        "/".join(f"{p}.{s}" for p, s in chain) for chain in chains
    ]
    if parent_path_annotation:
        wanted = parent_path_segments(parent_path_annotation)
        for chain in chains:
            if len(chain) != len(wanted):
                continue
            if all(
                (cls_q is None or cls_q == p) and slot_q == s
                for (cls_q, slot_q), (p, s) in zip(wanted, chain)
            ):
                return chain
        raise ValueError(
            f"Class {class_name!r} declares "
            f"`openapi.parent_path: {parent_path_annotation!r}` but no "
            f"matching chain exists. Candidates: {candidates_qualified}."
        )
    raise ValueError(
        f"Class {class_name!r} is reachable via multiple parent chains. "
        f"Pick one with the `openapi.parent_path` class annotation, "
        f"e.g. `openapi.parent_path: {candidates_qualified[0]!r}`. "
        f"Candidates: {candidates_qualified}."
    )


def parse_path_param_sources(class_name: str, raw: str) -> dict[str, tuple[str, str]]:
    """Parse `openapi.path_param_sources` into `{name: (Class, slot)}`."""
    sources: dict[str, tuple[str, str]] = {}
    for raw_entry in _parse_csv(raw):
        if ":" not in raw_entry:
            raise ValueError(
                f"Class {class_name!r} has malformed "
                f"`openapi.path_param_sources` entry {raw_entry!r}: "
                "expected `name:Class.slot`."
            )
        name, source = (s.strip() for s in raw_entry.split(":", 1))
        if "." not in source:
            raise ValueError(
                f"Class {class_name!r} has malformed source "
                f"{source!r} for parameter {name!r}: expected "
                "`Class.slot`."
            )
        src_class, src_slot = (s.strip() for s in source.split(".", 1))
        if not name or not src_class or not src_slot:
            raise ValueError(
                f"Class {class_name!r} has empty token in "
                f"`openapi.path_param_sources` entry {raw_entry!r}."
            )
        if name in sources:
            raise ValueError(
                f"Class {class_name!r} declares duplicate path "
                f"parameter {name!r} in `openapi.path_param_sources`."
            )
        sources[name] = (src_class, src_slot)
    return sources


def render_chain_hops(
    sv: SchemaView,
    chain: list[tuple[str, str]],
    *,
    class_path_id_name: Callable[[str], str],
    get_path_segment: Callable[[ClassDefinition], str],
    render_slot_segment: Callable[[ClassDefinition | None, SlotDefinition], str],
    identifier_slot: Callable[[str], SlotDefinition | None],
    induced_slots_by_name: Callable[[str], dict[str, SlotDefinition]],
) -> list[ChainHop]:
    """Translate a `[(parent_class, slot_name), ...]` chain into ChainHops.

    Each hop carries the rendered URL segments and the typed identifier
    slot for the parent. The calling generator decides how to format the
    final URL string and parameter list — this helper just supplies the
    typed pieces.
    """
    hops: list[ChainHop] = []
    for parent_name, slot_name in chain:
        id_slot = identifier_slot(parent_name)
        if id_slot is None:
            raise ValueError(
                f"Parent class {parent_name!r} in a deep nested chain "
                "has no identifier slot — can't synthesise its path "
                "parameter."
            )
        parent_cls = sv.get_class(parent_name)
        slot_def = induced_slots_by_name(parent_name).get(slot_name)
        slot_seg = (
            render_slot_segment(parent_cls, slot_def)
            if slot_def is not None
            else slot_name
        )
        hops.append(
            ChainHop(
                parent_class=parent_name,
                parent_id_param_name=class_path_id_name(parent_name),
                parent_path_segment=get_path_segment(parent_cls),
                slot_segment=slot_seg,
                parent_id_slot=id_slot,
            )
        )
    return hops
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_chains_helper.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/linkml_openapi/_chains.py tests/test_chains_helper.py
git commit -m "$(cat <<'EOF'
feat(_chains): extract shared helper for parent-chain detection

build_parent_chains_index, canonical_parent_chain, parent_path_segments,
parse_path_param_sources, render_chain_hops — pure helpers consumed by
both the OpenAPI generator and the Spring emitter. Owns chain validation
errors so wording stays identical across renderers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.2: Refactor OpenAPI generator to use the chains helper

**Files:**
- Modify: `src/linkml_openapi/generator.py:2235-2456` (`_collect_parent_chains`, `_canonical_parent_chain`, `_parent_path_segments`, `_parse_path_param_sources`, `_build_chain_path_params`)

- [ ] **Step 1: Add the import**

Add to the top of `src/linkml_openapi/generator.py`:

```python
from linkml_openapi._chains import (
    build_parent_chains_index,
    canonical_parent_chain,
    parent_path_segments as _parent_path_segments_helper,
    parse_path_param_sources as _parse_path_param_sources_helper,
    render_chain_hops,
    PATH_TEMPLATE_PLACEHOLDER_RE,
    ChainHop,
)
```

- [ ] **Step 2: Replace `_collect_parent_chains` body**

```python
def _collect_parent_chains(self) -> dict[str, list[list[tuple[str, str]]]]:
    return build_parent_chains_index(
        self.schemaview,
        resource_classes=set(self._get_resource_classes()),
        excluded_classes=self._excluded_classes,
        is_slot_excluded=self._is_slot_excluded,
        get_slot_annotation=self._get_slot_annotation,
        induced_slots=lambda name: list(self._induced_slots_iter(name)),
    )
```

- [ ] **Step 3: Replace `_canonical_parent_chain` body**

```python
def _canonical_parent_chain(self, class_name: str) -> list[tuple[str, str]]:
    cls = self.schemaview.get_class(class_name)
    annotated = (
        self._class_annotation(cls, "openapi.parent_path") if cls else None
    )
    return canonical_parent_chain(
        class_name, self._parent_chains_index, annotated
    )
```

- [ ] **Step 4: Replace `_parent_path_segments` static method**

```python
@staticmethod
def _parent_path_segments(annotation: str) -> list[tuple[str | None, str]]:
    return _parent_path_segments_helper(annotation)
```

(Keep the wrapper rather than delete callers — it's a private static method, removing entirely is fine if no callers remain. Verify with `grep`.)

- [ ] **Step 5: Replace `_parse_path_param_sources` static method**

```python
@staticmethod
def _parse_path_param_sources(class_name: str, raw: str) -> dict[str, tuple[str, str]]:
    return _parse_path_param_sources_helper(class_name, raw)
```

- [ ] **Step 6: Replace `_PATH_TEMPLATE_PLACEHOLDER_RE` class attribute**

In `OpenAPIGenerator`, find:

```python
_PATH_TEMPLATE_PLACEHOLDER_RE: ClassVar[re.Pattern[str]] = re.compile(r"\{([^{}]+)\}")
```

Replace with:

```python
_PATH_TEMPLATE_PLACEHOLDER_RE = PATH_TEMPLATE_PLACEHOLDER_RE
```

- [ ] **Step 7: Run the full OpenAPI test suite**

```bash
cd /Users/jackhiggs/workspace/linkml-openapi
pytest tests/test_generator.py tests/test_dcat3.py tests/test_post_processors.py -v
```

Expected: all tests pass. Behavior unchanged.

- [ ] **Step 8: Commit**

```bash
git add src/linkml_openapi/generator.py
git commit -m "$(cat <<'EOF'
refactor(generator): use _chains helper for parent-chain logic

_collect_parent_chains, _canonical_parent_chain, _parent_path_segments,
and _parse_path_param_sources delegate to the shared helper module.
Behavior unchanged — full OpenAPI test suite green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3 — Spring query params

### Task 3.1: Create the query-params test fixture

**Files:**
- Create: `tests/fixtures/spring_query_params.yaml`

- [ ] **Step 1: Write the fixture**

```yaml
# tests/fixtures/spring_query_params.yaml
id: https://example.org/spring_qp
name: spring_qp
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string

classes:
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people
    attributes:
      id:
        identifier: true
        range: string
        required: true
      name:
        range: string
      age:
        range: integer
      email:
        range: string
      created:
        range: datetime
      active:
        range: boolean
      tags:
        range: string
        multivalued: true
    slot_usage:
      id:
        annotations:
          openapi.path_variable: "true"
      name:
        annotations:
          openapi.query_param: sortable
      age:
        annotations:
          openapi.query_param: comparable,sortable
      email:
        annotations:
          openapi.query_param: "false"
      created:
        annotations:
          openapi.query_param: comparable
      active:
        annotations:
          openapi.query_param: equality
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/spring_query_params.yaml
git commit -m "$(cat <<'EOF'
test(spring): add query-params fixture covering all capability tokens

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.2: Add `_query_param_dicts` and wire into top-level list op

**Files:**
- Modify: `src/linkml_openapi/spring/generator.py:491-566` (`_top_level_ops`) and elsewhere
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_linkml_spring.py`:

```python
# Add at the top with other imports
from linkml_openapi.spring import SpringServerGenerator

QP_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_query_params.yaml")


@pytest.fixture(scope="module")
def qp_files() -> dict:
    return SpringServerGenerator(QP_FIXTURE, package="io.example.qp").build()


class TestQueryParams:
    """Slot-driven @RequestParam emission on Spring controllers.

    Pins the exact wire shape the OpenAPI side advertises in the spec —
    same parameter names, same Java types per LinkML range, same
    capability surface (equality / comparable / sortable / opt-out)."""

    def test_equality_param_emitted(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert '@RequestParam(name = "active", required = false) Boolean active' in src

    def test_comparable_emits_four_suffix_params(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        # equality
        assert '@RequestParam(name = "age", required = false) Long age' in src
        # comparison ops — wire name keeps __, java name camelCases
        assert '@RequestParam(name = "age__gte", required = false) Long ageGte' in src
        assert '@RequestParam(name = "age__lte", required = false) Long ageLte' in src
        assert '@RequestParam(name = "age__gt", required = false) Long ageGt' in src
        assert '@RequestParam(name = "age__lt", required = false) Long ageLt' in src

    def test_sortable_emits_single_list_string_param(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert (
            '@RequestParam(name = "sort", required = false) java.util.List<String> sort'
            in src
        )

    def test_query_param_false_excludes_slot(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert '"email"' not in src   # no @RequestParam(name = "email", ...)

    def test_query_param_java_types_per_range(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        # datetime range → OffsetDateTime
        assert (
            '@RequestParam(name = "created__gte", required = false) '
            'java.time.OffsetDateTime createdGte' in src
        )
        # boolean range → Boolean
        assert 'Boolean active' in src
        # integer range → Long
        assert 'Long age' in src

    def test_paging_params_still_present(self, qp_files):
        """Sanity: limit/offset stay even when slot-driven params are added."""
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert 'name = "limit"' in src
        assert 'name = "offset"' in src
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams -v
```

Expected: every test fails — Spring isn't emitting slot-driven params yet.

- [ ] **Step 3: Add `_query_param_dicts` and helpers to `SpringServerGenerator`**

Append these methods to `SpringServerGenerator` in `src/linkml_openapi/spring/generator.py` (place them after the existing `_list_query_params` helper at module scope is fine, but prefer methods on the class for `self._sv` access). Add them just after `_nested_ops`:

```python
def _query_param_dicts(
    self, cls: ClassDefinition, imports: set[str]
) -> list[dict]:
    """Slot-driven @RequestParam dicts for a list endpoint on `cls`.

    Returned dicts have the same shape as `_list_query_params()` so the
    api.java.jinja template renders them without changes. Limit/offset
    are NOT included here — those still come from `_list_query_params`.
    """
    surface = walk_query_params(
        self._sv,
        cls,
        schema_auto_default=self._schema_auto_query_params(),
        is_slot_excluded=lambda s: False,
        induced_slots=self._induced_slots,
        get_slot_annotation=self._get_slot_annotation_compat,
        get_class_annotation=self._class_annotation,
    )
    out: list[dict] = []
    for spec in surface.params:
        out.extend(self._render_query_param_spec(spec, imports))
    if surface.sort_tokens:
        out.append(self._render_sort_param())
        imports.add("java.util.List")
    return out


def _schema_auto_query_params(self) -> bool:
    raw = None
    sv_schema = self._sv.schema
    if sv_schema and sv_schema.annotations:
        for ann in sv_schema.annotations.values():
            if ann.tag == "openapi.auto_query_params":
                raw = str(ann.value)
                break
    if raw is None:
        return True
    return raw.strip().lower() in ("true", "1", "yes")


def _get_slot_annotation_compat(
    self, cls: ClassDefinition, slot_name: str, tag: str
) -> str | None:
    """Read a slot annotation, walking slot_usage on the class.

    Mirrors the OpenAPI generator's _get_slot_annotation but uses the
    Spring emitter's induced-slot cache."""
    if cls.slot_usage:
        items = (
            cls.slot_usage.values()
            if isinstance(cls.slot_usage, dict)
            else cls.slot_usage
        )
        for su in items:
            su_obj = su if not isinstance(su, str) else None
            if su_obj and getattr(su_obj, "name", None) == slot_name:
                anns = getattr(su_obj, "annotations", None)
                if anns:
                    for ann in (
                        anns.values() if isinstance(anns, dict) else [anns]
                    ):
                        if hasattr(ann, "tag") and ann.tag == tag:
                            return str(ann.value)
    induced = self._slot_for(cls, slot_name)
    if induced is not None:
        anns = getattr(induced, "annotations", None)
        if anns:
            keys = anns.values() if isinstance(anns, dict) else [anns]
            for ann in keys:
                if hasattr(ann, "tag") and ann.tag == tag:
                    return str(ann.value)
    top_level = self._sv.get_slot(slot_name)
    if top_level and top_level.annotations:
        for ann in top_level.annotations.values():
            if ann.tag == tag:
                return str(ann.value)
    return None


def _render_query_param_spec(
    self, spec: QueryParamSpec, imports: set[str]
) -> list[dict]:
    """Render one QueryParamSpec into one or more @RequestParam dicts."""
    out: list[dict] = []
    java_type = self._java_type_for_range(spec.slot, imports)
    if "equality" in spec.capabilities:
        out.append(
            {
                "annotation": (
                    f'@RequestParam(name = "{spec.slot.name}", '
                    "required = false)"
                ),
                "java_type": java_type,
                "java_name": _java_identifier(spec.slot.name),
            }
        )
    if "comparable" in spec.capabilities:
        for op in ("gte", "lte", "gt", "lt"):
            wire_name = f"{spec.slot.name}__{op}"
            java_name = (
                _java_identifier(spec.slot.name)
                + op[0].upper()
                + op[1:]
            )
            out.append(
                {
                    "annotation": (
                        f'@RequestParam(name = "{wire_name}", '
                        "required = false)"
                    ),
                    "java_type": java_type,
                    "java_name": java_name,
                }
            )
    return out


def _render_sort_param(self) -> dict:
    return {
        "annotation": '@RequestParam(name = "sort", required = false)',
        "java_type": "java.util.List<String>",
        "java_name": "sort",
    }


def _java_type_for_range(
    self, slot: SlotDefinition, imports: set[str]
) -> str:
    """Java type for a query-param @RequestParam, derived from slot range.

    Reuses _RANGE_TYPE_MAP. Class-ranged or enum-ranged slots fall back
    to String for query-param purposes — query params are wire-level
    scalars, and class refs in URL params are IRIs that bind as String."""
    range_name = slot.range or "string"
    if self._sv.get_class(range_name) is not None:
        return "String"
    mapping = _RANGE_TYPE_MAP.get(range_name)
    if mapping is None:
        return "String"
    java_inner, importable = mapping
    if importable:
        imports.add(importable)
    return java_inner
```

- [ ] **Step 4: Wire the helper into `_top_level_ops` list operation**

In `_top_level_ops` (line 491), find:

```python
{
    "javadoc": f"GET /{path_segment} — list {cn}s. Default returns 501.",
    "method_annotations": [f"@GetMapping(value = {base}, produces = {produces})"],
    "method_name": f"list{cn}s",
    "return_type": list_return,
    "params": _list_query_params(),
},
```

Change `"params": _list_query_params(),` to:

```python
"params": _list_query_params() + self._query_param_dicts(cls, imports),
```

`_top_level_ops` already accepts `imports` as a parameter — just thread it through. Verify by looking at the current signature.

Also add the imports at the top of `spring/generator.py`:

```python
from linkml_openapi._query_params import QueryParamSpec, walk_query_params
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
feat(spring): emit slot-driven @RequestParam on top-level list endpoints

Adds _query_param_dicts() and helpers, threading walk_query_params()
output through to the existing _list_query_params() in _top_level_ops.
Honors equality / comparable (four __gte/__lte/__gt/__lt) / sortable
(single List<String> sort) / "false" opt-out.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.3: Wire query params into composition list op

**Files:**
- Modify: `src/linkml_openapi/spring/generator.py:604-677` (`_composition_ops`)
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing test**

Append to `TestQueryParams` in `tests/test_linkml_spring.py`:

```python
def test_query_params_attached_to_composition_list(self, files):
    """Dataset.distribution is inlined: true → composition list at
    /datasets/{id}/distribution. The list endpoint carries Distribution's
    query params (auto-inferred from Distribution's scalar slots)."""
    src = files["io/example/dcat/api/DatasetApi.java"]
    # Distribution has `title` and other scalar slots that should
    # auto-infer as equality query params on the composition list.
    # Pin only the ones we know exist on the dcat3 fixture.
    assert "/datasets/{id}/distribution" in src
    # The composition list method should have at least one slot-driven
    # @RequestParam beyond limit/offset.
    list_method_start = src.find('listDatasetDistribution')
    list_method_end = src.find(') {', list_method_start)
    list_method_signature = src[list_method_start:list_method_end]
    # Count @RequestParam occurrences in this method's signature.
    assert list_method_signature.count("@RequestParam") >= 3   # limit, offset, + at least one slot
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams::test_query_params_attached_to_composition_list -v
```

Expected: FAIL — composition list still only has limit/offset.

- [ ] **Step 3: Wire query params into `_composition_ops`**

In `_composition_ops` (line 604), find the list op:

```python
{
    "javadoc": f"GET /{slot.name} — list embedded {tn}s under a {pn}.",
    "method_annotations": [f"@GetMapping(value = {collection}, produces = {produces})"],
    "method_name": f"list{pn}{sn}",
    "return_type": f"List<{tn}>",
    "params": [
        _path_param("id"),
        *_list_query_params(),
    ],
},
```

Change the `params` list to:

```python
"params": [
    _path_param("id"),
    *_list_query_params(),
    *self._query_param_dicts(target, imports),
],
```

The composition list lands on the `target` class — that's whose query params should appear.

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams::test_query_params_attached_to_composition_list -v
```

Expected: PASS.

- [ ] **Step 5: Run the full Spring test suite**

```bash
pytest tests/test_linkml_spring.py -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
feat(spring): emit composition-list @RequestParam from target class

Composition list endpoints (/<parent>/{id}/<slot>) now carry the
target class's query params — the addressable list of composed
sub-resources gets the same filter/sort surface a flat list would.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.4: Wire query params into reference list op

**Files:**
- Modify: `src/linkml_openapi/spring/generator.py:679-744` (`_reference_ops`)
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing test**

```python
def test_query_params_attached_to_reference_list(self, files):
    """Catalog.dataset is inlined: false → reference list at
    /catalogs/{id}/dataset. The list of attached IRIs carries Dataset's
    query params (filter the attached set by Dataset's scalars)."""
    src = files["io/example/dcat/api/CatalogApi.java"]
    list_method_start = src.find('listCatalogDatasetRefs')
    list_method_end = src.find(') {', list_method_start)
    list_method_signature = src[list_method_start:list_method_end]
    assert list_method_signature.count("@RequestParam") >= 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams::test_query_params_attached_to_reference_list -v
```

Expected: FAIL.

- [ ] **Step 3: Wire query params into `_reference_ops`**

In `_reference_ops` (line 679), find the list op and change the `params` to:

```python
"params": [
    _path_param("id"),
    *_list_query_params(),
    *self._query_param_dicts(target, imports),
],
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams::test_query_params_attached_to_reference_list -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
feat(spring): emit reference-list @RequestParam from target class

Reference list endpoints (/<parent>/{id}/<slot>) now carry the
referenced target class's query params, parity with the OpenAPI side.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.5: Validation tests for warns/raises

**Files:**
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Add validation tests to `TestQueryParams`**

```python
def test_unknown_query_param_token_warns(self, tmp_path):
    """Typoed capability token warns and is ignored. The slot still
    auto-infers as equality."""
    fixture = tmp_path / "schema.yaml"
    fixture.write_text("""\
id: https://example.org/qp_warn
name: qp_warn
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id: { identifier: true, range: string, required: true }
      name: { range: string }
    slot_usage:
      name:
        annotations:
          openapi.query_param: sorteable
""")
    with pytest.warns(UserWarning, match="sorteable"):
        SpringServerGenerator(str(fixture), package="io.example.qp_warn").build()


def test_sortable_on_multivalued_raises(self, tmp_path):
    fixture = tmp_path / "schema.yaml"
    fixture.write_text("""\
id: https://example.org/qp_err
name: qp_err
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: string, multivalued: true }
    slot_usage:
      tags:
        annotations:
          openapi.query_param: sortable
""")
    with pytest.raises(ValueError, match="multivalued"):
        SpringServerGenerator(str(fixture), package="io.example.qp_err").build()
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestQueryParams::test_unknown_query_param_token_warns tests/test_linkml_spring.py::TestQueryParams::test_sortable_on_multivalued_raises -v
```

Expected: 2 passed (no implementation needed — the shared helper already raises and warns).

- [ ] **Step 3: Commit**

```bash
git add tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
test(spring): pin warn/raise behavior on query-param annotations

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 4 — Spring deep nested chains

### Task 4.0: Add path-style support (kebab-case + slot path_segment overrides)

The Spring emitter needs to honor `openapi.path_style: kebab-case` at the schema level and `openapi.path_segment` at the slot level so URLs like `/data-services/{id}/web-resources/{id}` work. Mirrors what the OpenAPI generator does today; this lands before deep-chain emission so chain URLs use the correct segments.

**Files:**
- Modify: `src/linkml_openapi/spring/generator.py`
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_linkml_spring.py`:

```python
class TestPathStyle:
    """openapi.path_style: kebab-case + per-slot openapi.path_segment.
    Spring's auto-derived class path segments and slot segments respect
    the active path style; explicit per-class openapi.path values are
    taken verbatim (no transformation)."""

    def test_kebab_case_class_path_for_camelcase_class(self, tmp_path):
        fixture = tmp_path / "kebab.yaml"
        fixture.write_text("""\
id: https://example.org/k
name: k
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
annotations:
  openapi.path_style: kebab-case
classes:
  DataService:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.k").build()
        src = files["io/example/k/api/DataServiceApi.java"]
        assert '@GetMapping(value = "/data-services",' in src
        assert '@GetMapping(value = "/data-services/{id}",' in src

    def test_explicit_openapi_path_taken_verbatim(self, tmp_path):
        """openapi.path on a class is verbatim — no path-style transform."""
        fixture = tmp_path / "verbatim.yaml"
        fixture.write_text("""\
id: https://example.org/v
name: v
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path: my-custom-path
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.v").build()
        src = files["io/example/v/api/FooApi.java"]
        assert '@GetMapping(value = "/my-custom-path",' in src

    def test_slot_path_segment_override(self, tmp_path):
        """openapi.path_segment on a slot is taken verbatim and lands on
        nested URLs even when the slot identifier in the model stays
        snake_case."""
        fixture = tmp_path / "ps.yaml"
        fixture.write_text("""\
id: https://example.org/ps
name: ps
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Hub:
    annotations: { openapi.resource: "true", openapi.path: hubs }
    attributes:
      id: { identifier: true, range: string, required: true }
      web_resources:
        range: WebResource
        multivalued: true
        inlined: true
    slot_usage:
      web_resources:
        annotations:
          openapi.path_segment: "web-resources"
  WebResource:
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.ps").build()
        src = files["io/example/ps/api/HubApi.java"]
        assert '@GetMapping(value = "/hubs/{id}/web-resources",' in src

    def test_kebab_applied_to_chain_segments(self, tmp_path):
        """Schema-level kebab-case applies to auto-derived chain
        segments too — /data-services/{data_service_id}/sub-things/{id}."""
        fixture = tmp_path / "k_chain.yaml"
        fixture.write_text("""\
id: https://example.org/kc
name: kc
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
annotations:
  openapi.path_style: kebab-case
classes:
  DataService:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      sub_things:
        range: SubThing
        multivalued: true
  SubThing:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.kc").build()
        src = files["io/example/kc/api/SubThingApi.java"]
        # data_service_id stays snake_case (it's an identifier, not a URL noun);
        # the URL nouns "data-services" and "sub-things" both use kebab.
        assert '@GetMapping(value = "/data-services/{data_service_id}/sub-things/{id}"' in src
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_linkml_spring.py::TestPathStyle -v
```

Expected: 4 failures.

- [ ] **Step 3: Add path-style support to Spring**

In `src/linkml_openapi/spring/generator.py`, add the helper methods on `SpringServerGenerator` (place them near `_path_segment`):

```python
def _resolve_path_style(self) -> str:
    """Active schema-level path style; defaults to snake_case."""
    sv_schema = self._sv.schema
    if sv_schema and sv_schema.annotations:
        for ann in sv_schema.annotations.values():
            if ann.tag == "openapi.path_style":
                value = str(ann.value).strip().lower()
                if value in ("snake_case", "kebab-case"):
                    return value
    return "snake_case"


def _apply_path_style(self, name: str) -> str:
    """Render a name in the active path style.

    snake_case → unchanged. kebab-case → underscores become hyphens.
    Names with neither convention pass through (no character to swap).
    """
    if self._resolve_path_style() == "kebab-case":
        return name.replace("_", "-")
    return name
```

Replace the existing `_path_segment` method:

```python
def _path_segment(self, cls: ClassDefinition) -> str:
    """URL path segment for a class.

    Priority:
    1. `openapi.path` annotation — taken verbatim, no path-style transform.
    2. Auto-derived `<class_snake>s` with active path-style applied.
    """
    explicit = self._class_annotation(cls, "openapi.path")
    if explicit:
        return explicit.strip()
    return self._apply_path_style(_to_snake_case(cls.name) + "s")
```

Replace `_render_slot_segment_compat` with the path-style-aware version:

```python
def _render_slot_segment_compat(
    self, parent_cls: ClassDefinition | None, slot: SlotDefinition
) -> str:
    """Slot segment for chain / nested URLs.

    Priority:
    1. `openapi.path_segment` slot annotation — verbatim.
    2. Slot name with active path-style applied.
    """
    if parent_cls is not None:
        explicit = self._get_slot_annotation_compat(
            parent_cls, slot.name, "openapi.path_segment"
        )
        if explicit:
            return explicit.strip()
    return self._apply_path_style(slot.name)
```

Update `_nested_ops` to use `_render_slot_segment_compat` for the slot URL segment. Find:

```python
slot_seg = slot.name
collection = f'"/{path_segment}/{{id}}/{slot_seg}"'
item = f'"/{path_segment}/{{id}}/{slot_seg}/{target_id_path}"'
```

Replace with:

```python
slot_seg = self._render_slot_segment_compat(cls, slot)
collection = f'"/{path_segment}/{{id}}/{slot_seg}"'
item = f'"/{path_segment}/{{id}}/{slot_seg}/{target_id_path}"'
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestPathStyle -v
```

Expected: 4 passed.

- [ ] **Step 5: Run the full Spring test suite**

```bash
pytest tests/test_linkml_spring.py -v
```

Expected: all green. The dcat3 fixture doesn't set `openapi.path_style` so all existing tests behave identically.

- [ ] **Step 6: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
feat(spring): honor openapi.path_style and openapi.path_segment

Schema-level openapi.path_style: kebab-case renders auto-derived
class path segments (/data-services) and slot segments
(/hubs/{id}/web-resources) with hyphens. openapi.path on a class and
openapi.path_segment on a slot remain verbatim overrides — no transform.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.1: Add chains index cache and `_deep_chained_ops` skeleton

**Files:**
- Modify: `src/linkml_openapi/spring/generator.py`
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_linkml_spring.py`:

```python
class TestDeepChainedPaths:
    """Deep nested URL chains land on the leaf class's controller as
    item-only operations (read/update/delete) on the deep item path.
    Mirrors the OpenAPI generator's _emit_chained_deep_path which calls
    only _attach_item_operations."""

    def test_deep_path_emits_on_distribution_api(self, files):
        """The dcat3 fixture has Catalog.dataset (singular slot name)
        and Dataset.distribution (singular). Default path_id is
        <class_snake>_id for ancestors. The leaf parameter is {id} to
        match Spring's existing flat-URL convention (consistent with
        _top_level_ops which uses /{path_segment}/{{id}})."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution/{id}"'
            in src
        )

    def test_deep_method_name_via_chain_suffix(self, files):
        src = files["io/example/dcat/api/DistributionApi.java"]
        assert "getDistributionViaCatalogDataset" in src
        assert "updateDistributionViaCatalogDataset" in src
        assert "deleteDistributionViaCatalogDataset" in src

    def test_dataset_chain_depth_one(self, files):
        """Dataset's parent chain is just [Catalog.dataset] — a single
        hop. The deep URL is /catalogs/{catalog_id}/dataset/{id}."""
        src = files["io/example/dcat/api/DatasetApi.java"]
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{id}"'
            in src
        )
        assert "getDatasetViaCatalog" in src

    def test_deep_path_params_in_order(self, files):
        """Path parameters declared root → leaf in the method signature."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        get_idx = src.find("getDistributionViaCatalogDataset")
        sig_end = src.find(") {", get_idx)
        signature = src[get_idx:sig_end]
        # catalog_id appears before dataset_id appears before id
        assert (
            signature.index('"catalog_id"')
            < signature.index('"dataset_id"')
            < signature.index('"id"')
        )

    def test_deep_chained_url_is_item_only(self, files):
        """No collection-level GET/POST on the deep chained URL — list
        ops live on the parent's single-level nested URL."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        # No POST or list-shaped GET on the chain prefix
        # (i.e. without the trailing leaf {id}).
        assert (
            '@PostMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution",'
            not in src
        )
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution",'
            not in src
        )

    def test_no_method_name_collision_between_flat_and_deep_ops(self, files):
        """Within DistributionApi, all method names must be unique."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        import re
        method_names = re.findall(r"default ResponseEntity<[^>]*> (\w+)\(", src)
        assert len(method_names) == len(set(method_names)), (
            f"duplicate methods: {[m for m in method_names if method_names.count(m) > 1]}"
        )

    def test_ancestor_path_id_annotation_honored(self, tmp_path):
        """When an ancestor class declares openapi.path_id, the chain
        URL uses that value instead of the <class_snake>_id default.
        (Leaf parameter still {id}, matching Spring's flat-URL convention.)"""
        fixture = tmp_path / "path_id.yaml"
        fixture.write_text("""\
id: https://example.org/pid
name: pid
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
      openapi.path_id: catId
    attributes:
      id: { identifier: true, range: string, required: true }
      datasets:
        range: Dataset
        multivalued: true
  Dataset:
    annotations:
      openapi.resource: "true"
      openapi.path: datasets
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.pid").build()
        src = files["io/example/pid/api/DatasetApi.java"]
        assert '@GetMapping(value = "/catalogs/{catId}/datasets/{id}"' in src
        assert '@PathVariable("catId")' in src
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pytest tests/test_linkml_spring.py::TestDeepChainedPaths -v
```

Expected: 4 failures — Spring isn't emitting deep paths yet.

- [ ] **Step 3: Add chains index cache + `_deep_chained_ops`**

In `src/linkml_openapi/spring/generator.py`, add the import at the top:

```python
from linkml_openapi._chains import (
    build_parent_chains_index,
    canonical_parent_chain,
    parse_path_param_sources,
    render_chain_hops,
    PATH_TEMPLATE_PLACEHOLDER_RE,
    ChainHop,
)
```

In `__post_init__`, after the existing setup, add:

```python
self._chains_index = build_parent_chains_index(
    self._sv,
    resource_classes=self._resource_class_names(),
    excluded_classes=set(),
    is_slot_excluded=lambda s: False,
    get_slot_annotation=self._get_slot_annotation_compat,
    induced_slots=self._induced_slots,
)
```

Add the supporting method:

```python
def _resource_class_names(self) -> set[str]:
    out: set[str] = set()
    for name in self._sv.all_classes():
        cls = self._sv.get_class(name)
        if cls and self._is_resource(cls):
            out.add(name)
    return out
```

Add the deep-chained ops method:

```python
def _deep_chained_ops(
    self,
    cls: ClassDefinition,
    chain: list[tuple[str, str]],
    imports: set[str],
    media_types: list[str],
) -> list[dict]:
    """Item-only CRUD on the deep chained URL.

    Parity with OpenAPI's _emit_chained_deep_path: only read / update /
    delete attach to the deep item path. The deep collection list is
    NOT emitted — that surface lives on the parent controller's
    single-level nested list (already emitted by _nested_ops on the
    parent's controller)."""
    hops = render_chain_hops(
        self._sv,
        chain,
        class_path_id_name=self._class_path_id_name,
        get_path_segment=self._path_segment_for_class,
        render_slot_segment=self._render_slot_segment_compat,
        identifier_slot=self._identifier_slot_for,
        induced_slots_by_name=self._induced_slots_by_name,
    )
    cn = cls.name

    # Build URL: <hop[0].parent_path>/{<hop[0].id>}/<hop[0].slot>/{<hop[1].id>}/.../<hop[-1].slot>/{id}
    #
    # Each ChainHop carries:
    #   - parent_path_segment: the parent class's URL noun (only used for hop[0])
    #   - parent_id_param_name: snake_case path_id for this parent
    #   - slot_segment: the slot from this parent leading to the next hop
    #
    # The leaf id stays {id} to match Spring's existing flat-URL convention
    # in _top_level_ops (which uses /{path_segment}/{{id}} regardless of
    # openapi.path_id on the class). Ancestor segments DO honor path_id.
    parts: list[str] = [
        f"{hops[0].parent_path_segment}/{{{hops[0].parent_id_param_name}}}"
    ]
    for i in range(len(hops) - 1):
        parts.append(
            f"{hops[i].slot_segment}/{{{hops[i + 1].parent_id_param_name}}}"
        )
    parts.append(f"{hops[-1].slot_segment}/{{id}}")
    chain_url = "/".join(parts)
    deep_item = f'"/{chain_url}"'

    suffix = "Via" + "".join(_camel(p) for p, _ in chain)
    produces = _produces_arg(media_types)
    consumes = produces

    chain_path_params = [
        {
            "annotation": f'@PathVariable("{h.parent_id_param_name}")',
            "java_type": "String",
            "java_name": _java_identifier(h.parent_id_param_name),
        }
        for h in hops
    ]
    leaf_path_param = {
        "annotation": '@PathVariable("id")',
        "java_type": "String",
        "java_name": "id",
    }

    return [
        {
            "javadoc": f"GET deep — read a {cn} via its parent chain.",
            "method_annotations": [
                f"@GetMapping(value = {deep_item}, produces = {produces})"
            ],
            "method_name": f"get{cn}{suffix}",
            "return_type": cn,
            "params": [*chain_path_params, leaf_path_param],
        },
        {
            "javadoc": f"PUT deep — replace a {cn} via its parent chain.",
            "method_annotations": [
                f"@PutMapping(value = {deep_item}, consumes = {consumes}, produces = {produces})"
            ],
            "method_name": f"update{cn}{suffix}",
            "return_type": cn,
            "params": [
                *chain_path_params,
                leaf_path_param,
                {"annotation": "@RequestBody", "java_type": cn, "java_name": "body"},
            ],
        },
        {
            "javadoc": f"DELETE deep — delete a {cn} via its parent chain.",
            "method_annotations": [f"@DeleteMapping({deep_item})"],
            "method_name": f"delete{cn}{suffix}",
            "return_type": "Void",
            "params": [*chain_path_params, leaf_path_param],
        },
    ]
```

Add the supporting helper methods on `SpringServerGenerator`:

```python
def _class_path_id_name(self, class_name: str) -> str:
    """Honor openapi.path_id; fall back to <class_snake>_id."""
    cls = self._sv.get_class(class_name)
    if cls is not None:
        explicit = self._class_annotation(cls, "openapi.path_id")
        if explicit:
            return explicit.strip()
    return f"{_to_snake_case(class_name)}_id"


def _path_segment_for_class(self, cls: ClassDefinition) -> str:
    return self._path_segment(cls)


# `_render_slot_segment_compat` is added in Task 4.0 (path-style support).
# It applies `openapi.path_segment` slot overrides verbatim and falls back
# to `_apply_path_style(slot.name)`.


def _identifier_slot_for(self, class_name: str) -> SlotDefinition | None:
    for s in self._induced_slots(class_name):
        if s.identifier:
            return s
    return None


def _induced_slots_by_name(self, class_name: str) -> dict[str, SlotDefinition]:
    return {s.name: s for s in self._induced_slots(class_name)}
```

- [ ] **Step 4: Wire `_deep_chained_ops` into `_render_api`**

In `_render_api` (line 449), find:

```python
ops.extend(self._top_level_ops(cls, path_segment, imports, media_types))
ops.extend(self._nested_ops(cls, path_segment, imports, media_types))
```

Replace with:

```python
ops.extend(self._top_level_ops(cls, path_segment, imports, media_types))
ops.extend(self._nested_ops(cls, path_segment, imports, media_types))

# Deep nested chain (auto-derived). Item-only CRUD on the deep URL.
parent_path_ann = self._class_annotation(cls, "openapi.parent_path")
chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
if chain:
    ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestDeepChainedPaths -v
```

Expected: 4 passed.

- [ ] **Step 6: Run the full test suite**

```bash
pytest tests/test_linkml_spring.py -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
feat(spring): emit deep chained item-only paths on leaf controllers

DistributionApi (and any leaf class with a parent chain) now carries
read / update / delete on the canonical deep URL like
/catalogs/{catalogId}/datasets/{datasetId}/distributions/{distId}.
Parity with the OpenAPI generator's _emit_chained_deep_path: item-only
emission, deep collection list lives on the parent controller's
single-level nested list.

Method names get a `Via<Chain>` suffix to stay unique within the
controller interface.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.2: `nested_only` and `flat_only` gating

**Files:**
- Create: `tests/fixtures/spring_nested_only.yaml`
- Modify: `src/linkml_openapi/spring/generator.py`
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Create the fixture**

```yaml
# tests/fixtures/spring_nested_only.yaml
id: https://example.org/spring_no
name: spring_no
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string

classes:
  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
    attributes:
      id: { identifier: true, range: string, required: true }
      datasets:
        range: Dataset
        multivalued: true
      tags:
        range: Tag
        multivalued: true

  Dataset:
    annotations:
      openapi.resource: "true"
      openapi.path: datasets
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }

  Tag:
    annotations:
      openapi.resource: "true"
      openapi.path: tags
      openapi.flat_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
```

- [ ] **Step 2: Write the failing tests**

```python
NO_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_nested_only.yaml")


@pytest.fixture(scope="module")
def no_files() -> dict:
    return SpringServerGenerator(NO_FIXTURE, package="io.example.no_").build()


class TestNestedOnlyAndFlatOnly:
    def test_nested_only_suppresses_flat_ops(self, no_files):
        src = no_files["io/example/no_/api/DatasetApi.java"]
        # No flat /datasets endpoints.
        assert '@GetMapping(value = "/datasets",' not in src
        assert '@GetMapping(value = "/datasets/{id}",' not in src
        # Deep chain endpoint IS present (slot `datasets` plural in this
        # fixture; ancestor uses default snake_case path_id; leaf is {id}).
        assert '/catalogs/{catalog_id}/datasets/{id}' in src

    def test_flat_only_suppresses_deep_ops(self, no_files):
        src = no_files["io/example/no_/api/TagApi.java"]
        # Flat endpoints present.
        assert '@GetMapping(value = "/tags",' in src
        assert '@GetMapping(value = "/tags/{id}",' in src
        # Deep chain endpoint NOT present.
        assert '/catalogs/{catalog_id}/tags/{id}' not in src

    def test_nested_only_and_flat_only_together_raises(self, tmp_path):
        fixture = tmp_path / "bad.yaml"
        fixture.write_text("""\
id: https://example.org/bad
name: bad
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.nested_only: "true"
      openapi.flat_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="mutually exclusive"):
            SpringServerGenerator(str(fixture), package="io.example.bad").build()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_linkml_spring.py::TestNestedOnlyAndFlatOnly -v
```

Expected: 3 failures.

- [ ] **Step 4: Add gating to `_render_api`**

In `_render_api`, replace the chain emission block from Task 4.1 with:

```python
# Mutex check.
nested_only_raw = self._class_annotation(cls, "openapi.nested_only")
flat_only_raw = self._class_annotation(cls, "openapi.flat_only")
nested_only = nested_only_raw is not None and nested_only_raw.strip().lower() in ("true", "1", "yes")
flat_only = flat_only_raw is not None and flat_only_raw.strip().lower() in ("true", "1", "yes")
if nested_only and flat_only:
    raise ValueError(
        f"Class {cls.name!r} declares both `openapi.nested_only: \"true\"` "
        f"and `openapi.flat_only: \"true\"`. They are mutually exclusive — "
        "pick one. `nested_only` keeps the deep URL only; `flat_only` keeps "
        "the flat URL only."
    )

if not nested_only:
    ops.extend(self._top_level_ops(cls, path_segment, imports, media_types))
ops.extend(self._nested_ops(cls, path_segment, imports, media_types))

if not flat_only:
    parent_path_ann = self._class_annotation(cls, "openapi.parent_path")
    chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
    if chain:
        ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestNestedOnlyAndFlatOnly -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the full test suite**

```bash
pytest tests/test_linkml_spring.py -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py tests/fixtures/spring_nested_only.yaml
git commit -m "$(cat <<'EOF'
feat(spring): honor openapi.nested_only / flat_only with mutex check

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.3: Ambiguous chain validation

**Files:**
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing test**

```python
class TestAmbiguousChain:
    def test_ambiguous_chain_without_parent_path_raises(self, tmp_path):
        fixture = tmp_path / "ambig.yaml"
        fixture.write_text("""\
id: https://example.org/amb
name: amb
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: Tag, multivalued: true }
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: Tag, multivalued: true }
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="multiple parent chains"):
            SpringServerGenerator(str(fixture), package="io.example.amb").build()
```

- [ ] **Step 2: Run test to verify it passes (no implementation needed)**

```bash
pytest tests/test_linkml_spring.py::TestAmbiguousChain -v
```

Expected: PASS — `canonical_parent_chain` already raises this from the shared helper.

- [ ] **Step 3: Commit**

```bash
git add tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
test(spring): pin ambiguous-parent-chain ValueError

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 5 — Spring templated paths

### Task 5.1: Templated path emission with `ViaTemplate` suffix

**Files:**
- Create: `tests/fixtures/spring_path_template.yaml`
- Modify: `src/linkml_openapi/spring/generator.py`
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Create the fixture**

```yaml
# tests/fixtures/spring_path_template.yaml
id: https://example.org/spring_tpl
name: spring_tpl
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string

classes:
  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
    attributes:
      id: { identifier: true, range: string, required: true }

  ResourceVersion:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/catalogs/{cId}/resources/by-doi/{doi}/{version}"
      openapi.path_param_sources: "cId:Catalog.id,doi:ResourceVersion.doi,version:ResourceVersion.version"
      openapi.nested_only: "true"
    attributes:
      doi:     { identifier: true, range: string, required: true }
      version: { range: string, required: true }
```

- [ ] **Step 2: Write the failing tests**

```python
TPL_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_path_template.yaml")


@pytest.fixture(scope="module")
def tpl_files() -> dict:
    return SpringServerGenerator(TPL_FIXTURE, package="io.example.tpl").build()


class TestTemplatedPaths:
    def test_template_emits_verbatim_url(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert (
            '@GetMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}/{version}"'
            in src
        )

    def test_template_method_name_via_template_suffix(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert "getResourceVersionViaTemplate" in src
        assert "updateResourceVersionViaTemplate" in src
        assert "deleteResourceVersionViaTemplate" in src

    def test_template_path_params_typed_from_sources(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        # All three sources resolve to string-ranged slots → String params.
        assert '@PathVariable("cId") String cId' in src
        assert '@PathVariable("doi") String doi' in src
        assert '@PathVariable("version") String version' in src

    def test_template_collection_emitted_when_ending_in_placeholder(self, tpl_files):
        """The template ends with /{version}; default-on collection emits
        list/create at /v2/catalogs/{cId}/resources/by-doi/{doi}."""
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert (
            '@GetMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}",'
            in src
        )
        assert (
            '@PostMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}",'
            in src
        )

    def test_template_placeholder_source_mismatch_raises(self, tmp_path):
        fixture = tmp_path / "tpl_bad.yaml"
        fixture.write_text("""\
id: https://example.org/tpl_bad
name: tpl_bad
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/{a}/{b}"
      openapi.path_param_sources: "a:Catalog.id"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="don't match"):
            SpringServerGenerator(str(fixture), package="io.example.tpl_bad").build()

    def test_template_unknown_source_class_raises(self, tmp_path):
        fixture = tmp_path / "tpl_unknown.yaml"
        fixture.write_text("""\
id: https://example.org/tpl_u
name: tpl_u
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/{a}"
      openapi.path_param_sources: "a:DoesNotExist.id"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="unknown class"):
            SpringServerGenerator(str(fixture), package="io.example.tpl_u").build()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_linkml_spring.py::TestTemplatedPaths -v
```

Expected: 6 failures.

- [ ] **Step 4: Add `_deep_templated_ops`**

Append to `SpringServerGenerator`:

```python
def _deep_templated_ops(
    self,
    cls: ClassDefinition,
    template: str,
    imports: set[str],
    media_types: list[str],
) -> list[dict]:
    """Item-only CRUD on a templated deep URL, plus optional collection
    when the template ends with /{name}."""
    sources_raw = self._class_annotation(cls, "openapi.path_param_sources") or ""
    sources = parse_path_param_sources(cls.name, sources_raw)
    placeholders = list(PATH_TEMPLATE_PLACEHOLDER_RE.findall(template))
    unique_placeholders = list(dict.fromkeys(placeholders))

    if len(unique_placeholders) != len(placeholders):
        duplicates = sorted({p for p in placeholders if placeholders.count(p) > 1})
        raise ValueError(
            f"Class {cls.name!r} `openapi.path_template` "
            f"{template!r} repeats placeholder(s) {duplicates}: "
            "OpenAPI requires unique parameter names per path."
        )

    missing = set(unique_placeholders) - set(sources)
    extra = set(sources) - set(unique_placeholders)
    if missing or extra:
        raise ValueError(
            f"Class {cls.name!r} `openapi.path_template` placeholders "
            f"don't match `openapi.path_param_sources`. "
            f"Template placeholders: {sorted(unique_placeholders)!r}. "
            f"Source keys: {sorted(sources)!r}. "
            f"Missing sources: {sorted(missing)!r}. "
            f"Extra sources (not in template): {sorted(extra)!r}."
        )

    sv = self._sv

    def _param_dict_for_placeholder(name: str) -> dict:
        src_class, src_slot = sources[name]
        if sv.get_class(src_class) is None:
            raise ValueError(
                f"Class {cls.name!r} `openapi.path_param_sources` refers to "
                f"unknown class {src_class!r} for parameter {name!r}."
            )
        slot = self._induced_slots_by_name(src_class).get(src_slot)
        if slot is None:
            raise ValueError(
                f"Class {cls.name!r} `openapi.path_param_sources` refers to "
                f"unknown slot {src_class}.{src_slot!r} for parameter {name!r}."
            )
        return {
            "annotation": f'@PathVariable("{name}")',
            "java_type": self._java_type_for_range(slot, imports),
            "java_name": _java_identifier(name),
        }

    item_params = [_param_dict_for_placeholder(n) for n in unique_placeholders]
    cn = cls.name
    suffix = "ViaTemplate"
    deep_url = f'"{template}"'
    produces = _produces_arg(media_types)
    consumes = produces

    ops: list[dict] = [
        {
            "javadoc": f"GET {template} — read a {cn}.",
            "method_annotations": [
                f"@GetMapping(value = {deep_url}, produces = {produces})"
            ],
            "method_name": f"get{cn}{suffix}",
            "return_type": cn,
            "params": item_params,
        },
        {
            "javadoc": f"PUT {template} — replace a {cn}.",
            "method_annotations": [
                f"@PutMapping(value = {deep_url}, consumes = {consumes}, produces = {produces})"
            ],
            "method_name": f"update{cn}{suffix}",
            "return_type": cn,
            "params": [
                *item_params,
                {"annotation": "@RequestBody", "java_type": cn, "java_name": "body"},
            ],
        },
        {
            "javadoc": f"DELETE {template} — delete a {cn}.",
            "method_annotations": [f"@DeleteMapping({deep_url})"],
            "method_name": f"delete{cn}{suffix}",
            "return_type": "Void",
            "params": item_params,
        },
    ]

    # Collection emission when template ends with `/{name}`.
    coll_opt = self._class_annotation(cls, "openapi.path_template_collection")
    emit_collection = coll_opt is None or coll_opt.strip().lower() in ("true", "1", "yes")
    if emit_collection and template.endswith("}"):
        tail_open = template.rfind("/{")
        tail_name = template[tail_open + 2 : -1] if tail_open != -1 else None
        if tail_name and tail_name in {n for n in unique_placeholders}:
            collection_path = template[:tail_open]
            if collection_path:
                collection_params = [
                    p for p, n in zip(item_params, unique_placeholders) if n != tail_name
                ]
                ops.extend(
                    [
                        {
                            "javadoc": f"GET {collection_path} — list {cn}s.",
                            "method_annotations": [
                                f'@GetMapping(value = "{collection_path}", produces = {produces})'
                            ],
                            "method_name": f"list{cn}s{suffix}",
                            "return_type": f"List<{cn}>",
                            "params": [
                                *collection_params,
                                *_list_query_params(),
                                *self._query_param_dicts(cls, imports),
                            ],
                        },
                        {
                            "javadoc": f"POST {collection_path} — create a {cn}.",
                            "method_annotations": [
                                f'@PostMapping(value = "{collection_path}", consumes = {consumes}, produces = {produces})'
                            ],
                            "method_name": f"create{cn}{suffix}",
                            "return_type": cn,
                            "params": [
                                *collection_params,
                                {
                                    "annotation": "@RequestBody",
                                    "java_type": cn,
                                    "java_name": "body",
                                },
                            ],
                        },
                    ]
                )
    return ops
```

- [ ] **Step 5: Wire `_deep_templated_ops` into `_render_api`**

In `_render_api`, find:

```python
if not flat_only:
    parent_path_ann = self._class_annotation(cls, "openapi.parent_path")
    chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
    if chain:
        ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
```

Replace with:

```python
if not flat_only:
    template = self._class_annotation(cls, "openapi.path_template")
    if template:
        ops.extend(self._deep_templated_ops(cls, template, imports, media_types))
    else:
        parent_path_ann = self._class_annotation(cls, "openapi.parent_path")
        chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
        if chain:
            ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_linkml_spring.py::TestTemplatedPaths -v
```

Expected: 6 passed.

- [ ] **Step 7: Run the full Spring test suite**

```bash
pytest tests/test_linkml_spring.py -v
```

Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/linkml_openapi/spring/generator.py tests/test_linkml_spring.py tests/fixtures/spring_path_template.yaml
git commit -m "$(cat <<'EOF'
feat(spring): emit openapi.path_template URLs as @*Mapping with typed @PathVariable

ResourceVersion (and any class with openapi.path_template +
openapi.path_param_sources) gets the templated URL emitted verbatim
on its controller, with each {placeholder} bound to a typed
@PathVariable derived from the source slot's range. ViaTemplate suffix
keeps method names unique. Collection list/create emit by default when
the template ends with /{name}, opt-out via openapi.path_template_collection.

Validates: placeholder/source mismatch, unknown source class, malformed
sources entry — same wording as the OpenAPI generator (raised by the
shared parse_path_param_sources helper).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5.2: `path_template_collection: "false"` opt-out

**Files:**
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the failing test**

```python
def test_template_collection_opt_out(self, tmp_path):
    fixture = tmp_path / "tpl_no_coll.yaml"
    fixture.write_text("""\
id: https://example.org/tpl_nc
name: tpl_nc
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
  ResourceVersion:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/catalogs/{cId}/resources/{rId}"
      openapi.path_param_sources: "cId:Catalog.id,rId:ResourceVersion.id"
      openapi.path_template_collection: "false"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
    files = SpringServerGenerator(str(fixture), package="io.example.tpl_nc").build()
    src = files["io/example/tpl_nc/api/ResourceVersionApi.java"]
    # Item path emits.
    assert "/v2/catalogs/{cId}/resources/{rId}" in src
    # No collection /v2/catalogs/{cId}/resources GET or POST.
    assert '@GetMapping(value = "/v2/catalogs/{cId}/resources",' not in src
    assert '@PostMapping(value = "/v2/catalogs/{cId}/resources",' not in src
```

Add to `TestTemplatedPaths`.

- [ ] **Step 2: Run test to verify it passes (no implementation needed)**

```bash
pytest tests/test_linkml_spring.py::TestTemplatedPaths::test_template_collection_opt_out -v
```

Expected: PASS — opt-out logic was added in Task 5.1.

- [ ] **Step 3: Commit**

```bash
git add tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
test(spring): pin openapi.path_template_collection: "false" opt-out

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 6 — Drift detector

### Task 6.1: Parity test against the sidecar OpenAPI spec

**Files:**
- Modify: `tests/test_linkml_spring.py`

- [ ] **Step 1: Write the test**

```python
class TestParityWithOpenApiSide:
    """Belt-and-braces: every list-shaped Spring endpoint's @RequestParam
    wire-name set must equal the corresponding OpenAPI path's query-
    parameter name set. Catches future drift if shared helpers are ever
    bypassed by one renderer."""

    def test_query_param_wire_names_match_openapi_spec(self, tmp_path):
        import re
        import yaml as _yaml

        gen = SpringServerGenerator(FIXTURE, package="io.example.dcat")
        java_dir = tmp_path / "java"
        gen.emit(java_dir)
        spec_path = tmp_path / "resources" / "openapi.yaml"
        spec = _yaml.safe_load(spec_path.read_text())

        # For each spec path with a GET method that has query params,
        # find the matching Spring controller method and verify the set.
        request_param_re = re.compile(
            r'@RequestParam\(name = "([^"]+)"'
        )

        # Build {spec_path: set_of_query_param_names} for GET.
        spec_query_params: dict[str, set[str]] = {}
        for url, item in spec.get("paths", {}).items():
            get = item.get("get") if isinstance(item, dict) else None
            if not get:
                continue
            params = get.get("parameters") or []
            qp_names = {
                p["name"]
                for p in params
                if isinstance(p, dict) and p.get("in") == "query"
            }
            if qp_names:
                spec_query_params[url] = qp_names

        # For each Spring controller file, find @GetMapping value=URL and
        # the @RequestParam names in the same method.
        java_files = {
            relpath: source
            for relpath, source in gen.build().items()
            if relpath.endswith("Api.java")
        }

        # Collect spring {url: set_of_request_param_wire_names}
        getmapping_re = re.compile(
            r'@GetMapping\(value\s*=\s*"([^"]+)"[^)]*\)\s*\n[^\n]*\n\s*default ResponseEntity[^(]*\([^)]*\)',
            re.DOTALL,
        )
        spring_query_params: dict[str, set[str]] = {}
        for source in java_files.values():
            for m in getmapping_re.finditer(source):
                url = m.group(1)
                signature = m.group(0)
                wire_names = set(request_param_re.findall(signature))
                # Always-present pagination params don't count as query
                # parity — the OpenAPI spec emits them too, so they will
                # match. Keep them in the set.
                if wire_names:
                    spring_query_params.setdefault(url, set()).update(wire_names)

        # Walk every spec list endpoint and verify Spring matches.
        mismatches = []
        for url, spec_names in spec_query_params.items():
            spring_names = spring_query_params.get(url)
            if spring_names is None:
                # The Spring side may not emit single-level nested ops on
                # the leaf controller — OpenAPI emits them on the parent's
                # path. Skip URLs we couldn't find on the Spring side; the
                # purpose of this test is drift detection on shared paths.
                continue
            if spec_names != spring_names:
                mismatches.append(
                    f"{url}: spec={sorted(spec_names)}, spring={sorted(spring_names)}"
                )
        assert not mismatches, "Query-param drift detected:\n" + "\n".join(mismatches)
```

- [ ] **Step 2: Run the test**

```bash
pytest tests/test_linkml_spring.py::TestParityWithOpenApiSide -v
```

Expected: PASS. (If it fails, examine the mismatches output and adjust either the Spring renderer or the shared helper — most likely a per-slot annotation that one side reads differently.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_linkml_spring.py
git commit -m "$(cat <<'EOF'
test(spring): add belt-and-braces parity check vs sidecar OpenAPI spec

Walks every list-shaped GET in the generated openapi.yaml and asserts
the corresponding Spring controller's @RequestParam wire names match.
Catches future drift if either renderer ever bypasses the shared
helpers.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 7 — Final verification

### Task 7.1: Full test suite + linters green

**Files:**
- (none — this is verification)

- [ ] **Step 1: Run the full test suite**

```bash
cd /Users/jackhiggs/workspace/linkml-openapi
pytest tests/ -v
```

Expected: every test passes.

- [ ] **Step 2: Run ruff**

```bash
ruff check src/ tests/
```

Expected: no errors. Fix any flagged issues inline before continuing.

- [ ] **Step 3: Run ruff format**

```bash
ruff format src/ tests/
```

Expected: format applied. Stage and commit any formatter changes.

- [ ] **Step 4: Final commit (if formatter touched anything)**

```bash
git add -u
git commit -m "$(cat <<'EOF'
chore: ruff format

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Summary**

The Spring emitter now matches the OpenAPI generator on:

* Slot-driven query parameters (`equality` / `comparable` / `sortable`
  with auto-inference and opt-outs)
* Deep-chained URLs with `openapi.path_id`, `openapi.parent_path`,
  `openapi.nested_only`, `openapi.flat_only`
* Templated paths via `openapi.path_template` + `openapi.path_param_sources`,
  including default-on collection emission

Validation messages (warnings + ValueErrors) are identical across both
generators because they come from the shared `_query_params` and
`_chains` modules. The drift-detector test (`TestParityWithOpenApiSide`)
prevents future regressions.
