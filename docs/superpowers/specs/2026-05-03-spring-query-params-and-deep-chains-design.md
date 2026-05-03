# Spring emitter: query params and deep nested chains

**Date:** 2026-05-03
**Status:** Approved design — ready for implementation plan
**Repo:** `linkml-openapi`
**Branch baseline:** `main` @ `3b78dc2`

## Goal

Bring the LinkML → Spring server emitter (`src/linkml_openapi/spring/`) to
parity with the LinkML → OpenAPI generator on three surfaces:

1. **Query parameters** — `?slot=`, `?slot__gte=` / `__lte=` / `__gt=` /
   `__lt=`, and a single `?sort=` array param, driven by
   `openapi.query_param` capability tokens (`equality` / `comparable` /
   `sortable`) plus auto-inference from scalar slots, with class- and
   schema-level opt-out via `openapi.auto_query_params: "false"` and
   slot-level opt-out via `openapi.query_param: "false"`.
2. **Deep nested chains** — multi-hop URLs like
   `/catalogs/{catalog_id}/dataset/{dataset_id}/distribution/{id}`
   driven by the LinkML relationship graph, plus all four chain-shaping
   annotations: `openapi.path_id`, `openapi.parent_path`,
   `openapi.nested_only` / `openapi.flat_only`, and `openapi.path_template`
   with `openapi.path_param_sources`.
3. **Path style and slot-segment overrides** — schema-level
   `openapi.path_style: kebab-case` rendering hyphens for auto-derived
   class and slot URL nouns (`/data-services/{id}/web-resources`), and
   slot-level `openapi.path_segment` taken verbatim. Required so the
   chain URLs in #2 use the right segments.

The Spring emitter currently advertises a contract via the sidecar
OpenAPI spec that the controller code cannot honor: the spec emits deep
URLs and per-slot query params, but the controller stops at one level of
nesting and binds only `?limit=` / `?offset=`. Clients hitting the
documented URL shapes get 404s and silently dropped query params. This
design closes that drift.

## Non-goals

- **Patch endpoints on deep paths.** The current Spring emitter doesn't
  emit PATCH at all; that's a separate change.
- **Enum-typed query params.** Slot ranges that are LinkML enums bind
  as `String` in Java today (DTO fallback). Same fallback applies here;
  upgrading to generated enum types is a follow-up.
- **Nested query params on non-list nested operations.** Only list-shaped
  endpoints get slot-driven query params. POST / PUT / DELETE remain
  query-param-free aside from explicit annotations on the leaf class.
- **Response pagination shape.** `?limit=` / `?offset=` already emit; this
  spec doesn't change pagination semantics.

## Architecture

Two new modules under `src/linkml_openapi/`:

```
src/linkml_openapi/
├── _query_params.py     (NEW — shared)
├── _chains.py           (NEW — shared)
├── generator.py         (REFACTOR — thin renderers around helpers)
└── spring/
    └── generator.py     (EXTEND — call helpers, render Java)
```

Both helpers are pure: `SchemaView` plus the relevant class metadata in,
dataclasses out. Path-style and `path_id` rendering are injected via
callbacks because the OpenAPI generator already implements them on
`OpenAPIGenerator` and reuse beats re-extraction.

### `_query_params.py`

```python
from collections.abc import Callable
from dataclasses import dataclass

from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView


_QUERY_PARAM_TOKENS = frozenset({"equality", "comparable", "sortable"})
_COMPARABLE_RANGES = frozenset(
    {"integer", "float", "double", "decimal", "date", "datetime"}
)


@dataclass(frozen=True)
class QueryParamSpec:
    slot: SlotDefinition
    capabilities: frozenset[str]   # subset of _QUERY_PARAM_TOKENS


@dataclass(frozen=True)
class QueryParamSurface:
    params: list[QueryParamSpec]   # one per annotated/inferred slot
    sort_tokens: list[str]         # ["name", "-name", "age", "-age"] or []


def walk_query_params(
    sv: SchemaView,
    cls: ClassDefinition,
    *,
    schema_auto_default: bool,
    is_slot_excluded: Callable[[SlotDefinition], bool],
    induced_slots: Callable[[str], list[SlotDefinition]],
    get_slot_annotation: Callable[[ClassDefinition, str, str], str | None],
    get_class_annotation: Callable[[ClassDefinition, str], str | None],
) -> QueryParamSurface: ...
```

The helper:

* Reads `openapi.auto_query_params` at class level (falls back to
  `schema_auto_default`).
* For each induced slot of `cls`, parses `openapi.query_param` into a
  capability set.
* Warns on unknown tokens (`pytest.warns` testable).
* Raises `ValueError` on `sortable` + multivalued.
* Warns on `comparable` against a non-`_COMPARABLE_RANGES` slot.
* Auto-infers `{equality}` for non-multivalued, non-identifier scalar
  slots (string / integer / boolean / enum) when no explicit annotations
  exist on the class and auto-inference is enabled.

### `_chains.py`

```python
from collections.abc import Callable
from dataclasses import dataclass

from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView


@dataclass(frozen=True)
class ChainHop:
    parent_class: str
    parent_id_param_name: str    # honors openapi.path_id
    parent_path_segment: str     # honors openapi.path / path_style
    slot_segment: str            # honors openapi.path_segment / path_style
    parent_id_slot: SlotDefinition  # for typing the path param


def build_parent_chains_index(
    sv: SchemaView,
    *,
    resource_classes: set[str],
    excluded_classes: set[str],
    is_slot_excluded: Callable[[SlotDefinition], bool],
    get_slot_annotation: Callable[..., str | None],
    induced_slots: Callable[[str], list[SlotDefinition]],
) -> dict[str, list[list[tuple[str, str]]]]: ...


def canonical_parent_chain(
    class_name: str,
    index: dict[str, list[list[tuple[str, str]]]],
    parent_path_annotation: str | None,
) -> list[tuple[str, str]]: ...


def parse_path_param_sources(
    class_name: str, raw: str
) -> dict[str, tuple[str, str]]: ...


def render_chain_hops(
    sv: SchemaView,
    chain: list[tuple[str, str]],
    *,
    class_path_id_name: Callable[[str], str],
    get_path_segment: Callable[[ClassDefinition], str],
    render_slot_segment: Callable[[ClassDefinition, SlotDefinition], str],
    identifier_slot: Callable[[str], SlotDefinition | None],
) -> list[ChainHop]: ...
```

### Refactor scope on `generator.py`

The existing methods stay on the class but become 3–5-line wrappers:

* `_query_param_capabilities`, `_auto_query_params_enabled`,
  `_make_query_params` → call `walk_query_params(...)`, render to
  `Parameter` objects.
* `_collect_parent_chains`, `_canonical_parent_chain`,
  `_emit_chained_deep_path` → call shared functions, render to
  `PathItem` objects.

Behavior unchanged. Existing OpenAPI tests cover the regression surface;
the refactor is a separate commit ahead of any Spring change so a green
test run after the lift confirms parity is preserved before new code
lands.

## Spring rendering — query params

### New helper inside `spring/generator.py`

```python
def _query_param_dicts(self, cls: ClassDefinition) -> list[dict]:
    """Slot-driven additions to the @RequestParam list (limit/offset
    stay in _list_query_params)."""
    surface = walk_query_params(
        self._sv, cls,
        schema_auto_default=self._schema_auto_query_params(),
        is_slot_excluded=lambda s: False,
        induced_slots=self._induced_slots,
        get_slot_annotation=self._get_slot_annotation,
        get_class_annotation=self._class_annotation,
    )
    out: list[dict] = []
    for spec in surface.params:
        out.extend(self._render_query_param_spec(spec))
    if surface.sort_tokens:
        out.append(self._render_sort_param())
    return out
```

### Per-capability rendering

For a slot `age: integer` with `openapi.query_param: "comparable,sortable"`:

```java
@RequestParam(name = "age", required = false) Long age,
@RequestParam(name = "age__gte", required = false) Long ageGte,
@RequestParam(name = "age__lte", required = false) Long ageLte,
@RequestParam(name = "age__gt",  required = false) Long ageGt,
@RequestParam(name = "age__lt",  required = false) Long ageLt,
```

* Wire name (`name = "age__gte"`) is exactly what the OpenAPI spec
  advertises.
* Java identifier (`ageGte`) uses camelCase suffix because `age__gte`
  isn't a legal Java identifier and double underscores look hostile.

### Sort param

For any class with at least one `sortable` slot:

```java
@RequestParam(name = "sort", required = false) java.util.List<String> sort
```

`List<String>` because Spring's `@RequestParam` already splits comma-
separated values, and an enum-pinned Java type would (a) break on
`-name` (not a legal Java identifier) and (b) go stale on schema
changes. The enum constraint stays in the OpenAPI spec, where springdoc
reads it from the sidecar.

### Java type binding

| Slot range | Java type |
|---|---|
| `string` | `String` |
| `integer` | `Long` |
| `float` / `double` / `decimal` | `Float` / `Double` / `BigDecimal` |
| `boolean` | `Boolean` |
| `date` / `datetime` | `java.time.LocalDate` / `java.time.OffsetDateTime` |
| `uri` / `uriorcurie` / `nodeidentifier` | `java.net.URI` |
| Enum range | `String` (matches DTO fallback) |

Reuses the existing `_RANGE_TYPE_MAP` in `spring/generator.py`. Imports
flow through the same `imports: set[str]` accumulator `_render_api`
already maintains.

### Where the params attach

Slot-driven query params extend the existing `params` list at five call
sites:

| Call site | Endpoint shape | Source class |
|---|---|---|
| `_top_level_ops` `list` op | `GET /<segment>` | resource class itself |
| `_composition_ops` list op | `GET /<parent>/{id}/<slot>` | composed target class |
| `_reference_ops` list op | `GET /<parent>/{id}/<slot>` | referenced target class |
| **NEW** templated collection | `GET /<template-prefix>` | leaf class |

Every list-shaped endpoint that lands on a class gets *that class's*
query-param surface, regardless of how the URL was reached.

Note: auto-derived deep chained URLs are **item-only** (read / update /
delete on `/catalogs/{catalogId}/.../{distId}`) — they don't carry a
list endpoint, so there's no "deep list" row above. The list view of
distributions reachable through a chain is served by the parent's
single-level nested list (`/datasets/{id}/distribution` on
`DatasetApi`), and that endpoint already picks up the target class's
query params via the `_composition_ops` / `_reference_ops` rows. This
mirrors `_emit_chained_deep_path` on the OpenAPI side, which calls only
`_attach_item_operations`.

## Spring rendering — deep chains and templates

### Operation method naming

Java method names within a controller interface must be unique across
the leaf's flat + deep ops. Use the same suffix pattern as the OpenAPI
side, camelCased:

| OpenAPI `operationId` | Spring method name |
|---|---|
| `get_distribution` | `getDistribution` |
| `get_distribution_via_catalog_dataset` | `getDistributionViaCatalogDataset` |
| `list_distributions_via_catalog_dataset` | `listDistributionsViaCatalogDataset` |
| `get_resource_version_via_template` | `getResourceVersionViaTemplate` |

Suffix builder:

* Chains: `"Via" + "".join(camel(c) for c, _ in chain)`
* Templates: `"ViaTemplate"`

### Two new methods on `SpringServerGenerator`

```python
def _deep_chained_ops(
    self, cls: ClassDefinition, chain: list[tuple[str, str]],
    imports: set[str], media_types: list[str],
) -> list[dict]: ...


def _deep_templated_ops(
    self, cls: ClassDefinition, template: str,
    imports: set[str], media_types: list[str],
) -> list[dict]: ...
```

Wire-up in `_render_api`:

```python
nested_only = _is_truthy(self._class_annotation(cls, "openapi.nested_only") or False)
flat_only   = _is_truthy(self._class_annotation(cls, "openapi.flat_only")   or False)
if nested_only and flat_only:
    raise ValueError(
        f"Class {cls.name!r} declares both `openapi.nested_only: \"true\"` "
        f"and `openapi.flat_only: \"true\"`. They are mutually exclusive — "
        "pick one. `nested_only` keeps the deep URL only; `flat_only` keeps "
        "the flat URL only."
    )

if not nested_only:
    ops.extend(self._top_level_ops(cls, ...))
ops.extend(self._nested_ops(cls, ...))      # this class as parent — unaffected
if not flat_only:
    template = self._class_annotation(cls, "openapi.path_template")
    if template:
        ops.extend(self._deep_templated_ops(cls, template, imports, media_types))
    else:
        chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
        if chain:
            ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
```

`self._chains_index` is built once in `__post_init__` via
`build_parent_chains_index(...)` from the shared helper, mirroring the
OpenAPI generator's caching strategy.

### Deep-chained ops — emitted shape

For `Distribution` reachable via `Catalog.dataset / Dataset.distribution`:

```java
@GetMapping(
    value = "/catalogs/{catalogId}/datasets/{datasetId}/distributions/{distId}",
    produces = {"application/json", "application/ld+json", "text/turtle", "application/rdf+xml"}
)
ResponseEntity<Distribution> getDistributionViaCatalogDataset(
    @PathVariable("catalogId") String catalogId,
    @PathVariable("datasetId") String datasetId,
    @PathVariable("distId") String distId
) { ... }
```

Path parameter ordering: chain hops (root → direct parent) first, then
leaf id. `@PathVariable` wire names come from `openapi.path_id` (or
`<class_snake>_id` fallback). Java identifiers run through the existing
`_java_identifier`.

**Item-only emission.** Deep chained URLs carry only read / update /
delete operations on the deep item path — no list / create at this URL.
This mirrors `_emit_chained_deep_path` on the OpenAPI side (line 2478),
which calls only `_attach_item_operations`. The deep collection view
(`/catalogs/{catalogId}/datasets/{datasetId}/distributions`) is not
emitted by either generator; the addressable list endpoint for
chained-leaf classes is the parent controller's single-level nested
list (`/datasets/{id}/distribution` on `DatasetApi`), reached via
`_composition_ops` / `_reference_ops`.

### Templated ops — emitted shape

For `ResourceVersion` with
`openapi.path_template: "/v2/catalogs/{cId}/resources/by-doi/{doi}/{version}"`:

```java
@GetMapping(
    value = "/v2/catalogs/{cId}/resources/by-doi/{doi}/{version}",
    produces = {...}
)
ResponseEntity<ResourceVersion> getResourceVersionViaTemplate(
    @PathVariable("cId")     String cId,
    @PathVariable("doi")     String doi,
    @PathVariable("version") String version
) { ... }
```

Java parameter type per placeholder comes from the `Class.slot` source's
range via `_RANGE_TYPE_MAP`. Wire names taken verbatim from the
template. Java identifiers run through `_java_identifier`.

Collection emission: when the template ends with `/{name}`, the same
default-on rule as OpenAPI (`openapi.path_template_collection: "false"`
opts out) emits list / create on the template minus the trailing
segment. The collection list endpoint also picks up
`_query_param_dicts(cls)`.

## Validation — single source of truth

All validation lives in the shared helpers and raises/warns once. Both
generators inherit identical messages.

| Condition | Behavior | Raised by |
|---|---|---|
| Unknown `openapi.query_param` token | `warnings.warn` — token ignored | `_query_params.walk_query_params` |
| `comparable` on non-ordered range | `warnings.warn` — emits anyway | `_query_params.walk_query_params` |
| `sortable` on multivalued slot | `ValueError` | `_query_params.walk_query_params` |
| Multiple parent chains, no `openapi.parent_path` | `ValueError`, lists candidates | `_chains.canonical_parent_chain` |
| `openapi.parent_path` doesn't match any chain | `ValueError`, lists candidates | `_chains.canonical_parent_chain` |
| `openapi.nested_only` + `openapi.flat_only` both true | `ValueError` | each generator's `_render_api` / `_build_openapi` (cheap, local) |
| Template placeholder ↔ `path_param_sources` mismatch | `ValueError`, lists missing/extra | template walker |
| `path_param_sources` malformed entry | `ValueError`, quotes offending entry | `_chains.parse_path_param_sources` |
| `path_param_sources` references unknown class/slot | `ValueError` | template walker (uses SchemaView) |
| Duplicate placeholder in template | `ValueError` | template walker |

Wording stays exactly what the OpenAPI generator emits today —
existing tests pinning OpenAPI error messages keep working unchanged.

## Test plan

Three new test classes in `tests/test_linkml_spring.py`, plus targeted
fixtures for cases the existing `tests/fixtures/dcat3.yaml` doesn't
cover.

### `TestQueryParams` — `tests/fixtures/spring_query_params.yaml`

```yaml
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id:    { identifier: true, range: string, required: true }
      name:  { range: string }
      age:   { range: integer }
      email: { range: string }
      tags:  { range: string, multivalued: true }
    slot_usage:
      name:  { annotations: { openapi.query_param: sortable } }
      age:   { annotations: { openapi.query_param: comparable,sortable } }
      email: { annotations: { openapi.query_param: "false" } }
```

Tests:

* `test_equality_param_emitted` — `@RequestParam(name = "name", required = false) String name`
* `test_comparable_emits_four_suffix_params` — wire names `age__gte` / `__lte` / `__gt` / `__lt`, Java names `ageGte` / `ageLte` / `ageGt` / `ageLt`
* `test_sortable_emits_single_list_string_param` — `java.util.List<String> sort`
* `test_query_param_false_excludes_slot` — no `email` `@RequestParam`
* `test_auto_inferred_when_no_annotations` — bookstore-style fixture
* `test_auto_query_params_false_suppresses_inference` — class-level opt-out leaves only `limit`/`offset`
* `test_query_params_attached_to_composition_list` — params from target class on `/<parent>/{id}/<slot>` GET
* `test_query_params_attached_to_reference_list` — same on reference list
* `test_query_param_java_types_per_range` — `Long` / `Boolean` / `OffsetDateTime` / `String` per range
* `test_unknown_query_param_token_warns` — `pytest.warns(UserWarning, match=...)`
* `test_sortable_on_multivalued_raises` — `pytest.raises(ValueError, match=...)`

### `TestDeepChainedPaths` — reuses `dcat3.yaml`

Already declares `openapi.parent_path` on `Distribution`,
`DatasetSeries`, `DataService`, `CatalogRecord`, `Agent`.

Tests:

* `test_deep_path_emits_on_distribution_api`
* `test_deep_method_name_via_chain_suffix`
* `test_deep_path_params_in_order`
* `test_deep_path_id_honors_openapi_path_id`
* `test_deep_chained_url_is_item_only` — no `@GetMapping(value = "/catalogs/{catalogId}/datasets/{datasetId}/distributions"` (collection) on `DistributionApi`; only the `{distId}` item URL
* `test_no_method_name_collision_between_flat_and_deep_ops`
* `test_dataset_chain_depth_one`
* `test_nested_only_suppresses_flat_ops` — small fixture
* `test_flat_only_suppresses_deep_ops` — small fixture
* `test_nested_only_and_flat_only_together_raises` — small fixture
* `test_ambiguous_chain_without_parent_path_raises` — small fixture with two parent chains

### `TestTemplatedPaths` — `tests/fixtures/spring_path_template.yaml`

```yaml
classes:
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

Tests:

* `test_template_emits_verbatim_url`
* `test_template_method_name_via_template_suffix` — `getResourceVersionViaTemplate`
* `test_template_path_params_typed_from_sources`
* `test_template_collection_emitted_when_ending_in_placeholder`
* `test_template_collection_opt_out` — `openapi.path_template_collection: "false"`
* `test_template_placeholder_source_mismatch_raises`
* `test_template_unknown_source_class_raises`

### `TestParityWithOpenApiSide`

Belt-and-braces: pin that for every list-shaped Spring endpoint, the set
of `@RequestParam` wire names equals the set of OpenAPI spec query
parameter names on the same path.

* `test_query_param_wire_names_match_openapi_spec` — parses the sidecar
  spec, walks every `paths.*.{get|post}.parameters[in:query]`, finds
  the matching Spring `@RequestParam(name = "...")` lines, asserts
  equality.

## Risk surface

Two things to double-check during implementation.

1. **Refactoring the OpenAPI generator** — extracting
   `_collect_parent_chains` and friends into pure helpers without
   breaking the existing 100+ tests. **Mitigation:** lift in a separate
   commit before any Spring change; run the full OpenAPI test suite
   green before adding Spring code.
2. **Operation-id de-duplication on the Spring side** — Java method
   names need to be unique within an interface. The `_via_<chain>`
   suffix from the OpenAPI side handles it, but `_nested_ops` (single-
   level) lives on parent controllers, not the leaf, so there's no
   actual collision. **Mitigation:** regression test asserting no
   duplicate method names per controller file.

## Build sequence

Recommended order — each step verifiable independently:

1. **Lift `_query_params.py`.** Move `_query_param_capabilities`,
   `_auto_query_params_enabled`, the constants, plus a new
   `walk_query_params` factoring out the loop in `_make_query_params`.
   Replace the OpenAPI generator's three methods with thin wrappers.
   Run the OpenAPI test suite — should be green.
2. **Lift `_chains.py`.** Move `_collect_parent_chains`,
   `_canonical_parent_chain`, `_parent_path_segments`,
   `_parse_path_param_sources`. Add `render_chain_hops`. Replace the
   OpenAPI generator's bodies with wrappers. Run the OpenAPI test suite
   — should be green.
3. **Wire query params into Spring.** Add `_query_param_dicts`,
   `_render_query_param_spec`, `_render_sort_param`. Extend the five
   call sites. Add `TestQueryParams`. Should pass.
4. **Wire deep chains into Spring.** Add `_deep_chained_ops`,
   `_deep_templated_ops`, `_chains_index` cache, `nested_only` /
   `flat_only` gating. Add `TestDeepChainedPaths` and `TestTemplatedPaths`.
   Should pass.
5. **Add `TestParityWithOpenApiSide`.** Final belt-and-braces drift
   detector. Should pass without further changes.

Each step is a self-contained PR-sized chunk.
