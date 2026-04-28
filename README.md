# linkml-openapi

[![CI](https://github.com/jackhiggs/linkml-openapi/actions/workflows/ci.yml/badge.svg)](https://github.com/jackhiggs/linkml-openapi/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Generate [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) specifications from [LinkML](https://linkml.io/) schemas.

## Features

- Converts LinkML classes to OpenAPI component schemas (JSON Schema)
- Generates CRUD endpoints with path/query parameters
- Supports inheritance via `allOf` references
- Maps LinkML enums, ranges, constraints, and multivalued slots
- Annotation-driven control over resources, paths, operations, path variables, and query parameters
- CLI and Python API
- Registers as a LinkML generator plugin (`linkml.generators` entry point)

## Install

```bash
pip install linkml-openapi
```

## Usage

### CLI

```bash
# Generate OpenAPI YAML from a LinkML schema
gen-openapi schema.yaml > openapi.yaml

# JSON output
gen-openapi schema.yaml -f json > openapi.json

# Custom title, version, server
gen-openapi schema.yaml --api-title "My API" --api-version 2.0.0 --server-url https://api.example.com

# Only generate endpoints for specific classes
gen-openapi schema.yaml --classes Person --classes Address
```

### Python

```python
from linkml_openapi.generator import OpenAPIGenerator

gen = OpenAPIGenerator("schema.yaml", api_title="My API", server_url="https://api.example.com")
yaml_str = gen.serialize(format="yaml")
json_str = gen.serialize(format="json")
```

#### Generator options

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `api_title` | `str` | schema name | `info.title` in the spec |
| `api_version` | `str` | `"1.0.0"` | `info.version` in the spec |
| `server_url` | `str` | `"http://localhost:8000"` | `servers[0].url` in the spec |
| `resource_filter` | `list[str]` | `None` | Only generate endpoints for these classes |
| `format` | `str` | `"yaml"` | Output format: `"yaml"` or `"json"` |
| `openapi_version` | `str` | `"3.0.3"` | OpenAPI dialect to emit (`"3.0.3"` or `"3.1.0"`) |
| `flatten_inheritance` | `bool` | `False` | Inline parent properties instead of using `allOf` |
| `error_schema` | `bool` | `True` | Synthesize an RFC 7807 `Problem` schema and reference it from non-2xx responses |

> **Why default to 3.0.3?** Several popular codegens — notably
> `openapi-generator`'s Spring server library — still mishandle `allOf`-based
> inheritance under OpenAPI 3.1.0, silently producing duplicate `Foo_1`
> schemas. 3.0.3 round-trips the same schemas cleanly. Pass
> `--openapi-version 3.1.0` to opt into the newer dialect once your
> downstream tooling is ready.

> **`--flatten-inheritance`** inlines every inherited property directly into
> the subclass schema, so each component is self-contained and there is no
> `allOf` at all. Use it for codegens that still trip on inline-schema-inside
> -allOf, or whenever you prefer denormalized schemas.

## Annotations

All `openapi.*` annotations use LinkML's built-in `annotations` mechanism and do not require changes to the LinkML metamodel. Annotation values are strings. Boolean-like annotations use `"true"` / `"false"`.

### Schema-level annotations

Placed at the top of the schema, in the same `annotations:` block that LinkML
uses for schema-wide metadata.

#### `openapi.profile.<name>.<key>` — multi-view filtering

A single LinkML schema can drive multiple API surfaces (internal,
partner, external) by declaring named profiles, then activating one at
generation time. Each profile is encoded as flat dotted annotation
tags at the schema level:

```yaml
annotations:
  openapi.profile.external.description:    Public surface; PII hidden.
  openapi.profile.external.exclude_classes: AuditLog
  openapi.profile.external.exclude_slots:   internal_notes,pii_email,contributor_id

  openapi.profile.partner.description: Authenticated partner organisations.
  openapi.profile.partner.exclude_slots: internal_notes
```

| Key | Value | Effect |
|-----|-------|--------|
| `description` | string | Tagged into `info.description` of the generated spec. |
| `exclude_classes` | comma-separated class names | Removes the class from `components.schemas` and drops every endpoint emitted for it. Slots whose `range` is an excluded class are also dropped. |
| `exclude_slots` | comma-separated slot names | Removes the slot from every class schema (including via `is_a` inheritance) and from every nested-path / query-param walk. |
| `include_classes` / `include_slots` | comma-separated names | Reserved for whitelist semantics; not yet implemented. |

Activate a profile at generation time:

```bash
gen-openapi schema.yaml                       > openapi-internal.yaml   # full surface
gen-openapi schema.yaml --profile partner     > openapi-partner.yaml
gen-openapi schema.yaml --profile external    > openapi-external.yaml
```

Profile-restricted specs still carry valid `x-rdf-class` /
`x-rdf-property` extensions on every slot they *do* expose — the same
in-memory service can serve different audiences with faithful RDF
graphs from the same data.

**Drift detection.** A profile that excludes a slot annotated with
`openapi.path_variable` or `openapi.query_param` would silently emit
a broken spec — so the generator fails at generation time with the
exact remediation:

```
ValueError: Profile 'external' excludes slot 'id' on 'Item', but the
slot is annotated with openapi.path_variable. Remove the annotation,
drop the slot from exclude_slots, or exclude the whole class.
```

**Activating a non-declared profile** also fails loudly, listing the
profiles that *are* declared.

#### `openapi.error_class`

Names a class in the schema to use as the body of every non-2xx response,
**replacing the synthesized RFC 7807 `Problem`**. Used when an organisation
already has a standardised error envelope it wants every API to emit.

```yaml
annotations:
  openapi.error_class: ApiError

classes:
  ApiError:
    attributes:
      code:    { range: string, required: true }
      message: { range: string, required: true }
      trace_id: string
```

When omitted (the default), the generator synthesizes a `Problem` schema
matching [RFC 7807 — Problem Details for HTTP APIs](https://www.rfc-editor.org/rfc/rfc7807)
and references it from every 404 / 422 response.

The named class must exist in the schema; otherwise generation fails with
a clear error.

To opt out entirely (today's body-less responses), pass
`--no-error-schema` on the CLI or `error_schema=False` to the Python API.

### Class-level annotations

Annotations are placed in the `annotations` block of a class definition.

#### `openapi.resource`

Controls whether a class generates REST endpoints.

| Value | Behaviour |
|-------|-----------|
| `"true"` | Class generates CRUD endpoints |
| `"false"` or omitted | Class is excluded from endpoint generation |

**Resource selection logic:**

- If **no class** in the schema has `openapi.resource`, all non-abstract, non-mixin classes with attributes get endpoints (backwards-compatible default).
- If **any class** has `openapi.resource`, only classes with `openapi.resource: "true"` generate endpoints. This lets you opt in specific classes while excluding the rest.
- Mixin classes (`mixin: true`) are always excluded regardless of annotations.
- The `resource_filter` parameter / `--classes` CLI flag applies as an additional filter on top of annotation-based selection.

```yaml
classes:
  NamedThing:
    description: Abstract base - no endpoints generated
    slots: [id, name]

  Person:
    is_a: NamedThing
    annotations:
      openapi.resource: "true"  # This class gets endpoints
```

#### `openapi.path`

Sets a custom URL path segment for the resource's endpoints.

| Value | Example result |
|-------|----------------|
| `people` | `/people`, `/people/{id}` |
| `org/units` | `/org/units`, `/org/units/{id}` |
| omitted | Auto-pluralized snake_case: `Person` becomes `/persons` |

```yaml
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people     # GET /people, GET /people/{id}
```

#### `openapi.operations`

Comma-separated list of CRUD operations to generate. Controls which HTTP methods appear on the collection and item paths.

| Operation | HTTP method | Path | Description |
|-----------|-------------|------|-------------|
| `list` | `GET` | `/{path}` | List instances (supports query params) |
| `create` | `POST` | `/{path}` | Create a new instance |
| `read` | `GET` | `/{path}/{vars}` | Get a single instance by ID |
| `update` | `PUT` | `/{path}/{vars}` | Replace an instance |
| `patch` | `PATCH` | `/{path}/{vars}` | Partial update via JSON Merge Patch (RFC 7396) |
| `delete` | `DELETE` | `/{path}/{vars}` | Delete an instance |

Default when omitted: all CRUD operations except `patch` (`list,create,read,update,delete`). PATCH is opt-in.

When `patch` is included, the generator also emits a `<Class>Patch` schema in
`components.schemas`: a flat schema with every induced slot present and
optional, identifier excluded, `additionalProperties: false`, and
`x-rdf-class` / `x-rdf-property` extensions preserved. The PATCH request
body media type is fixed at `application/merge-patch+json` (RFC 7396 is
JSON-specific); the 200 response uses the class's `openapi.media_types` as
usual. Multivalued slots replace wholesale per RFC 7396 — that is the
spec's behaviour, not a generator quirk.

```yaml
  Person:
    annotations:
      openapi.resource: "true"
      openapi.operations: "list,read"   # Read-only: GET /people + GET /people/{id}
```

```yaml
  AuditLog:
    annotations:
      openapi.resource: "true"
      openapi.operations: "list"        # Collection-only, no item endpoint
```

#### Discriminator (polymorphism)

The generator emits an OpenAPI `discriminator` block on a parent schema
when either signal is present:

1. **LinkML-native** — a slot with `designates_type: true`. The slot
   becomes the discriminator field; concrete subclass instances default
   to the class name (LinkML's own behaviour).

   ```yaml
   classes:
     Animal:
       abstract: true
       attributes:
         species:
           designates_type: true
           range: string
     Dog:    { is_a: Animal }
     Cat:    { is_a: Animal }
   ```

2. **Existing-system override** — `openapi.discriminator: <field>` on the
   parent picks (or synthesizes) the field, and `openapi.type_value:
   <string>` on each subclass pins the wire value. Use this when you're
   adopting linkml-openapi against an existing API surface that already
   has a fixed field name and fixed values you can't change.

   ```yaml
   classes:
     Product:
       abstract: true
       annotations:
         openapi.discriminator: kind     # synthesizes the field if needed
       attributes:
         sku: { identifier: true, range: string, required: true }
     Book:
       is_a: Product
       annotations:
         openapi.type_value: BOOK        # not "Book"
       attributes:
         title: { range: string }
     Vinyl:
       is_a: Product
       annotations:
         openapi.type_value: VINYL
   ```

   produces

   ```yaml
   Product:
     properties:
       kind: { type: string, enum: [BOOK, VINYL] }
     required: [sku, kind]
     discriminator:
       propertyName: kind
       mapping:
         BOOK: '#/components/schemas/Book'
         VINYL: '#/components/schemas/Vinyl'
   Book:
     allOf:
       - $ref: '#/components/schemas/Product'
       - properties:
           kind: { type: string, enum: [BOOK], default: BOOK }
         required: [kind]
   ```

| Annotation | Where | Purpose |
|------------|-------|---------|
| `openapi.discriminator: <field>` | parent class | Pick or synthesise the discriminator field. Errors if the class also has `designates_type`. |
| `openapi.type_value: <string>` | concrete subclass | Override the default wire value (class name) for an existing-system match. |

Validation:

- `designates_type: true` and `openapi.discriminator` on the same class
  → generation error (they say the same thing two ways).
- Two subclasses with the same `openapi.type_value` in one discriminator
  group → generation error.
- Mixins are not part of the polymorphic mapping (they're trait
  composition, not subtyping).

Polymorphic endpoints fall out automatically: an abstract parent with
`openapi.resource: "true"` gets standard CRUD paths whose request /
response schemas `$ref` the parent — and the discriminator block on
the parent does the polymorphic dispatch at codegen / runtime.

#### `openapi.media_types`

Comma-separated list of media types each operation generated for the class
should advertise on its responses and request bodies.

| Value | Example result |
|-------|----------------|
| `"application/json"` | JSON only (default when omitted) |
| `"application/json,application/ld+json,text/turtle,application/rdf+xml"` | Every listed type appears under `responses[*].content` and `requestBody.content` |

The first listed type stays the default. Each operation's response (and the
request body, on `POST` / `PUT`) gets one `content` entry per media type, all
referencing the same component schema. Use this for RDF-shaped APIs (JSON-LD,
Turtle, RDF/XML) or any other content negotiation surface (CSV, NDJSON,
XML, …) — it removes the need for a postprocessor that fans out the content
blocks by hand.

```yaml
  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
      openapi.media_types: "application/json,application/ld+json,text/turtle,application/rdf+xml"
```

#### `x-rdf-class` / `x-rdf-property` extensions

The generator propagates LinkML's `class_uri` and `slot_uri` into the OpenAPI
output as `x-` extensions. CURIEs are expanded against the schema's `prefixes`
map; absolute IRIs are passed through verbatim. No annotation is needed —
this is automatic for any schema that already declares URIs.

```yaml
prefixes:
  schema: http://schema.org/

classes:
  Person:
    class_uri: schema:Person
    attributes:
      email:
        slot_uri: schema:email
```

produces:

```yaml
components:
  schemas:
    Person:
      type: object
      x-rdf-class: http://schema.org/Person
      properties:
        email:
          type: string
          x-rdf-property: http://schema.org/email
```

This lets RDF-aware downstream tools (SHACL generators, JSON-LD context
builders, Jena/RDF4J mappers) consume the OpenAPI spec directly without
needing the original LinkML source.

#### Nested paths from class-ranged slots

Multivalued slots whose `range` is another class get nested path
operations automatically — no annotation needed. The shape depends
entirely on what LinkML already says about the slot:

| LinkML signal | Semantics | Nested operations |
|---------------|-----------|-------------------|
| `inlined: true` (or target has no identifier) | **Composition** — child has no independent identity, lifecycle goes through the parent | full CRUD on `/{parent}/{id}/{slot}` and `/{slot}/{target_id}` |
| `inlined: false` (default when target has identifier) | **Reference** — child has its own lifecycle, the slot links to it | attach (`POST` with `ResourceLink`) on `/{parent}/{id}/{slot}`, detach (`DELETE`) on `/{slot}/{target_id}` |

Composition example — `Order.line_items` is inline:

```yaml
classes:
  Order:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      line_items:
        range: LineItem
        multivalued: true
        inlined: true
  LineItem:
    attributes:
      line_id: { identifier: true, range: string, required: true }
      sku:     { range: string }
```

emits

```
POST   /orders/{id}/line_items                 body: full LineItem
GET    /orders/{id}/line_items                 list
GET    /orders/{id}/line_items/{line_item_id}  read
PUT    /orders/{id}/line_items/{line_item_id}  replace
DELETE /orders/{id}/line_items/{line_item_id}  delete
```

Reference example — `Person.addresses` links to existing `Address` resources:

```yaml
classes:
  Person:
    annotations: { openapi.resource: "true" }
    attributes:
      id:        { identifier: true, range: string, required: true }
      addresses: { range: Address, multivalued: true }
  Address:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
```

emits

```
GET    /persons/{id}/addresses                  list attached
POST   /persons/{id}/addresses                  attach (body: ResourceLink or array)
DELETE /persons/{id}/addresses/{address_id}     detach (Address entity stays)
```

The shared `ResourceLink` component is added to `components.schemas`
only when at least one reference relationship is present. Attach body:

```json
{ "id": "https://example.org/addresses/42" }
```

or as a batch:

```json
[
  { "id": "https://example.org/addresses/42" },
  { "id": "https://example.org/addresses/43" }
]
```

The `Address` resource is mutated via `/addresses/{id}` — the nested
path manages the *link*, not the linked entity. Composition is the
opposite: the nested path *is* how the child is mutated, since it has
no independent flat path.

**Opt-out per slot.** Some multivalued class-ranged slots aren't
browseable collections — back-references, lookups, relationships
already exposed elsewhere. Suppress nested-path generation for an
individual slot with `openapi.nested: "false"`:

```yaml
Person:
  attributes:
    addresses: { range: Address, multivalued: true }   # default — nested paths emitted
    knows:     { range: Person,  multivalued: true }
  slot_usage:
    knows:
      annotations:
        openapi.nested: "false"                        # ← /persons/{id}/knows is NOT emitted
```

The slot still appears in the parent's component schema (so it
serializes / deserializes normally); only the nested-path operations
are skipped. The default remains on — `multivalued: true, range: Class`
already says "this is a collection," and the API exposes it unless you
say otherwise.

**Loud failure** — a class with `openapi.resource: "true"` and item-path
operations (`read`/`update`/`delete`) but no identifier slot raises at
generation time with an exact remediation message.

##### Inverse direction via LinkML's `inverse:`

LinkML slots are unidirectional. To get the reverse-direction nested
path *without* declaring a real slot on the other side, use LinkML's
existing `inverse:` field:

```yaml
classes:
  Article:
    annotations: { openapi.resource: "true" }
    attributes:
      doi: { identifier: true, range: string, required: true }
      reviewers:
        range: Reviewer
        multivalued: true
        inverse: Reviewer.articles      # ← Reviewer has no real `articles` slot
  Reviewer:
    annotations: { openapi.resource: "true" }
    attributes:
      reviewer_id: { identifier: true, range: string, required: true }
```

emits both directions:

```
GET    /articles/{doi}/reviewers                  # forward (real slot)
POST   /articles/{doi}/reviewers
DELETE /articles/{doi}/reviewers/{reviewer_id}

GET    /reviewers/{reviewer_id}/articles          # reverse (synthesised from inverse:)
POST   /reviewers/{reviewer_id}/articles
DELETE /reviewers/{reviewer_id}/articles/{article_id}
```

The synthesised reverse direction is always reference-shaped: it uses
the same `ResourceLink` attach / detach body as a real reference slot
would. Composition can't be inverted (a composed child has no
independent IRI to reference, so there's nothing to attach to from
the other side).

If both sides declare real slots that point at each other via
`inverse:`, no synthesis happens — each side emits naturally from its
own slot walk. The `inverse:` declaration is only the load-bearing
signal when one side wants the path without paying for a real slot.

**No name-based inference.** Without an `inverse:` declaration, the
generator never guesses that `Article.reviewers` implies a path on
`Reviewer`. That's a parallel-vocabulary trap.

### Slot-level annotations

Slot annotations are placed via `slot_usage` on the class (not on the top-level slot definition). This is because the same slot may serve different roles in different classes.

#### `openapi.format`

Override the OpenAPI `format` string for a slot's emitted schema. Useful
when the LinkML range alone doesn't carry enough information — for example
to mark an `integer` slot as `int64` (large byte sizes, epoch milliseconds,
high-cardinality IDs that overflow `Integer`) or to mark a `string` slot
as `binary` / `byte` / `password`.

| Slot range | Without annotation | With `openapi.format: int64` |
|------------|---------------------|------------------------------|
| `integer`  | `type: integer`     | `type: integer, format: int64` |
| `string`   | `type: string`      | `type: string, format: <value>` |

```yaml
slots:
  byte_size:
    range: integer
    annotations:
      openapi.format: int64       # avoids 32-bit overflow downstream

  avatar:
    range: string
    annotations:
      openapi.format: binary       # raw bytes, not text

  api_key:
    range: string
    annotations:
      openapi.format: password     # Swagger UI redacts
```

For multivalued slots, the format is applied to the array's `items`
schema, not the array itself (which has no `format` in OpenAPI).

The annotation accepts any string; no allow-list is enforced, so vendor
formats pass through unchanged.

#### `openapi.path_variable`

Marks a slot as a path variable in the item endpoint URL.

| Value | Behaviour |
|-------|-----------|
| `"true"` | Slot appears as `{slot_name}` in the item path; the parameter schema mirrors the slot's range (alias for `"iri"`) |
| `"iri"` | Same as `"true"` — preserves any `format: uri` typing from a uri-range slot |
| `"slug"` | Slot appears as `{slot_name}` but the parameter schema is plain `string`, regardless of the slot's range |
| omitted | Slot is not a path variable |

Use `"slug"` when the URL segment is a short identifier (`main`,
`uk-population-2026`) derived from the resource's IRI rather than the IRI
itself — the body still carries the absolute IRI in the same field. Use
`"iri"` (or `"true"`) when the URL segment is the full IRI (e.g. behind a
URL-encoding gateway).

When one or more slots are annotated as path variables, they replace the default identifier-based placeholder. Multiple path variables are joined in order: `/people/{id}/{version}`.

When no slots are annotated as path variables, the generator falls back to the class's identifier slot (or a slot named `id`) in `iri` mode.

```yaml
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people
    slot_usage:
      id:
        annotations:
          openapi.path_variable: "true"   # GET /people/{id}, schema mirrors slot range

  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
    attributes:
      id:
        identifier: true
        range: uri
    slot_usage:
      id:
        annotations:
          openapi.path_variable: slug     # GET /catalogs/{id}, schema is plain string
```

#### `openapi.query_param`

Marks a slot as a query parameter on the `list` operation. Accepts a
comma-separated set of capability tokens:

| Token | Effect |
|-------|--------|
| `"true"` / `"equality"` | `?slot=value` exact-match filter (today's behaviour) |
| `"comparable"` | adds `?slot__gte=` / `?slot__lte=` / `?slot__gt=` / `?slot__lt=`. Implies equality. |
| `"sortable"` | slot becomes a valid token in a single `?sort=` array parameter. Implies equality. |
| omitted | Slot is not a query parameter |

`comparable` and `sortable` imply `equality` — most APIs that filter by
range or sort by a field also accept exact-match.

```yaml
  Person:
    annotations:
      openapi.resource: "true"
    slot_usage:
      name:
        annotations:
          openapi.query_param: sortable               # ?name=Alice and ?sort=name,-name
      age:
        annotations:
          openapi.query_param: comparable,sortable    # ?age=, ?age__gte=, ?age__lte=, sort
```

emits these query params on `GET /persons` (in addition to `limit` / `offset`):

```
?name=                ?age=
?age__gte=            ?age__lte=            ?age__gt=            ?age__lt=
?sort=  (array, comma-separated, enum: [name, -name, age, -age])
```

The `?sort=` parameter uses `style: form, explode: false`, so multiple
sort tokens round-trip as `?sort=name,-age`.

**Validation:**

- `comparable` is only well-defined for ordered ranges (`integer`,
  `float`, `double`, `decimal`, `date`, `datetime`). Setting it on a
  string slot warns at generation time — lex comparison is rarely the
  intent.
- `sortable` on a multivalued slot is a generation error — sort order
  over a set isn't well-defined.

When no slots are annotated with `openapi.query_param`, the generator
auto-infers equality-only query parameters from all non-multivalued,
non-identifier slots with `string`, `integer`, `boolean`, or enum
ranges (backwards compatible).

For catalog-shaped classes with 30+ slots, that auto-inference produces
unusably noisy list endpoints. Three layered annotations let you turn
it off at whichever level matches your intent.

##### `openapi.auto_query_params` — schema or class level

Defaults to `"true"`. Set to `"false"` to suppress the auto-inference
entirely; only `limit`, `offset`, and explicitly annotated slots emit
as query parameters.

```yaml
# Schema-level: every class in the spec opts out of auto-inference.
annotations:
  openapi.auto_query_params: "false"

classes:
  Dataset:
    annotations:
      openapi.resource: "true"          # → /datasets?limit=&offset= only
    slot_usage:
      title:
        annotations: { openapi.query_param: equality }
      created:
        annotations: { openapi.query_param: comparable,sortable }
```

Class-level wins over schema-level, so a single class can opt back in
when the schema-level default is off:

```yaml
annotations:
  openapi.auto_query_params: "false"

classes:
  Tag:
    annotations:
      openapi.resource: "true"
      openapi.auto_query_params: "true"   # this class keeps auto-inference
```

##### `openapi.query_param: "false"` — slot level

Removes one slot from auto-inference even when auto is enabled — for
oversized strings, free-text descriptions, or fields you never want
to filter on:

```yaml
classes:
  Article:
    slot_usage:
      raw_blob:
        annotations:
          openapi.query_param: "false"   # excluded from /articles?... params
```

`limit` and `offset` pagination parameters are always included on list
endpoints regardless of any of these annotations.

### Annotation summary

| Annotation | Level | Values | Default behaviour |
|------------|-------|--------|-------------------|
| `openapi.resource` | class | `"true"` / `"false"` | All non-abstract, non-mixin classes |
| `openapi.path` | class | path segment string | Auto-pluralized snake_case of class name |
| `openapi.operations` | class | comma-separated list | `list,create,read,update,delete` |
| `openapi.media_types` | class | comma-separated list | `application/json` |
| `openapi.auto_query_params` | schema or class | `"true"` / `"false"` | `"true"` (auto-infer scalar slots) |
| `openapi.path_variable` | slot (via `slot_usage`) | `"true"` | Identifier slot |
| `openapi.query_param` | slot (via `slot_usage`) | `"true"` / token list / `"false"` | Auto-inferred from slot type |
| `openapi.format` | slot | format string | derived from slot range |

## Type Mapping

Slot `range` values are mapped to OpenAPI schema types for component schemas, path variables, and query parameters:

| LinkML Range | OpenAPI Type | Format |
|-------------|-------------|--------|
| `string` | `string` | |
| `integer` | `integer` | |
| `float` | `number` | `float` |
| `double` | `number` | `double` |
| `boolean` | `boolean` | |
| `date` | `string` | `date` |
| `datetime` | `string` | `date-time` |
| `uri` | `string` | `uri` |
| `uriorcurie` | `string` | `uri` |
| `decimal` | `number` | |
| `ncname` | `string` | |
| `nodeidentifier` | `string` | `uri` |
| Class reference | `$ref` to component schema | |
| Enum reference | `$ref` to component schema | |
| Multivalued slot | `array` of the above | |

## Constraints

LinkML slot constraints map to JSON Schema in component schemas:

| LinkML | JSON Schema |
|--------|------------|
| `required: true` | In `required` array |
| `pattern` | `pattern` |
| `minimum_value` | `minimum` |
| `maximum_value` | `maximum` |
| `identifier: true` | Path parameter (fallback) |
| `is_a` (inheritance) | `allOf` with `$ref` to parent |
| `multivalued: true` | `type: array` with `items` |
| `description` | `description` |

## Complete Example

```yaml
id: https://example.org/my-api
name: my_api_schema
title: My API

prefixes:
  linkml: https://w3id.org/linkml/

default_range: string

classes:
  NamedThing:
    abstract: true
    description: Abstract base class (no endpoints)
    attributes:
      id:
        identifier: true
        range: string
        required: true
      name:
        range: string
        required: true

  Person:
    is_a: NamedThing
    description: A person
    annotations:
      openapi.resource: "true"
      openapi.path: people
      openapi.operations: "list,read,create"
    attributes:
      age:
        range: integer
        minimum_value: 0
        maximum_value: 200
      email:
        range: string
        pattern: "^\\S+@\\S+\\.\\S+$"
      status:
        range: PersonStatus
    slot_usage:
      id:
        annotations:
          openapi.path_variable: "true"
      name:
        annotations:
          openapi.query_param: "true"
      age:
        annotations:
          openapi.query_param: "true"

  Address:
    description: A mailing address
    annotations:
      openapi.resource: "true"
      openapi.path: addresses
      openapi.operations: "list,read"
    attributes:
      id:
        identifier: true
        range: string
        required: true
      street:
        range: string
      city:
        range: string

enums:
  PersonStatus:
    permissible_values:
      ALIVE:
      DEAD:
      UNKNOWN:
```

This generates:

| Method | Path | Operation | Query params |
|--------|------|-----------|--------------|
| `GET` | `/people` | List people | `?name=`, `?age=`, `?limit=`, `?offset=` |
| `POST` | `/people` | Create person | |
| `GET` | `/people/{id}` | Get person | |
| `GET` | `/addresses` | List addresses | `?limit=`, `?offset=`, `?street=`, `?city=` |
| `GET` | `/addresses/{id}` | Get address | |

- `NamedThing` is excluded because it is abstract.
- `Person` has only `list`, `read`, `create` (no `update`/`delete`) due to `openapi.operations`.
- `Address` has only `list`, `read` due to `openapi.operations`.
- Person's query params are annotation-driven (`name`, `age`). Address has no `openapi.query_param` annotations, so params are auto-inferred.

## Examples

The `examples/` directory contains end-to-end examples with LinkML input schemas and their generated OpenAPI output:

| Example | Description |
|---------|-------------|
| [`petstore/`](examples/petstore/) | Classic API with custom paths, operation limiting, query params, and enums |
| [`bookstore/`](examples/bookstore/) | Inheritance (`is_a`), multivalued references, and constraints (`pattern`, `minimum_value`) |
| [`minimal/`](examples/minimal/) | Single class with zero annotations — shows auto-inferred endpoints and query params |

Each directory contains a `schema.yaml` (LinkML input) and `openapi.yaml` (generated output). Regenerate all outputs with:

```bash
bash examples/generate.sh
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check src/ tests/
ruff format src/ tests/
```

## License

MIT
