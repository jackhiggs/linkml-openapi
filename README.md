# linkml-openapi

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
gen-openapi schema.yaml -f json -o openapi.json

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

## Annotations

All `openapi.*` annotations use LinkML's built-in `annotations` mechanism and do not require changes to the LinkML metamodel. Annotation values are strings. Boolean-like annotations use `"true"` / `"false"`.

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
| `delete` | `DELETE` | `/{path}/{vars}` | Delete an instance |

Default when omitted: all five operations (`list,create,read,update,delete`).

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

### Slot-level annotations

Slot annotations are placed via `slot_usage` on the class (not on the top-level slot definition). This is because the same slot may serve different roles in different classes.

#### `openapi.path_variable`

Marks a slot as a path variable in the item endpoint URL.

| Value | Behaviour |
|-------|-----------|
| `"true"` | Slot appears as `{slot_name}` in the item path |
| omitted | Slot is not a path variable |

When one or more slots are annotated as path variables, they replace the default identifier-based placeholder. Multiple path variables are joined in order: `/people/{id}/{version}`.

When no slots are annotated as path variables, the generator falls back to the class's identifier slot (or a slot named `id`).

```yaml
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people
    slot_usage:
      id:
        annotations:
          openapi.path_variable: "true"   # GET /people/{id}
```

#### `openapi.query_param`

Marks a slot as a query parameter on the `list` operation.

| Value | Behaviour |
|-------|-----------|
| `"true"` | Slot appears as an optional query parameter on the collection `GET` |
| omitted | Slot is not a query parameter |

All annotated query parameters are generated as optional (`required: false`). The parameter schema type is derived from the slot's `range`.

When no slots are annotated with `openapi.query_param`, the generator auto-infers query parameters from all non-multivalued, non-identifier slots with `string`, `integer`, `boolean`, or enum ranges (backwards compatible).

`limit` and `offset` pagination parameters are always included on list endpoints regardless of annotations.

```yaml
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people
    slot_usage:
      name:
        annotations:
          openapi.query_param: "true"     # GET /people?name=Alice
      age_in_years:
        annotations:
          openapi.query_param: "true"     # GET /people?age_in_years=30
```

### Annotation summary

| Annotation | Level | Values | Default behaviour |
|------------|-------|--------|-------------------|
| `openapi.resource` | class | `"true"` / `"false"` | All non-abstract, non-mixin classes |
| `openapi.path` | class | path segment string | Auto-pluralized snake_case of class name |
| `openapi.operations` | class | comma-separated list | `list,create,read,update,delete` |
| `openapi.path_variable` | slot (via `slot_usage`) | `"true"` | Identifier slot |
| `openapi.query_param` | slot (via `slot_usage`) | `"true"` | Auto-inferred from slot type |

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

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check src/ tests/
ruff format src/ tests/
```
