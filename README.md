# linkml-openapi

Generate [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) specifications from [LinkML](https://linkml.io/) schemas.

## Features

- Converts LinkML classes to OpenAPI component schemas (JSON Schema)
- Generates CRUD endpoints with path/query parameters
- Supports inheritance via `allOf` references
- Maps LinkML enums, ranges, constraints, and multivalued slots
- Annotation-driven control over resources, paths, and operations
- CLI and Python API

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
gen-openapi schema.yaml --title "My API" --version 2.0.0 --server-url https://api.example.com

# Only generate endpoints for specific classes
gen-openapi schema.yaml --classes Person Address
```

### Python

```python
from linkml_runtime.utils.schemaview import SchemaView
from linkml_openapi.generator import OpenAPIGenerator

sv = SchemaView("schema.yaml")
generator = OpenAPIGenerator(sv, title="My API")
spec = generator.generate()  # dict
yaml_str = generator.serialize(format="yaml")
```

## Annotations

Control endpoint generation with LinkML annotations on classes:

```yaml
classes:
  Person:
    annotations:
      openapi.resource: "true"        # Generate CRUD endpoints for this class
      openapi.path: people             # Custom URL path segment (default: auto-pluralized snake_case)
      openapi.operations: "list,read"  # Limit operations (default: list,create,read,update,delete)
    attributes:
      id:
        identifier: true  # Becomes the {id} path parameter
```

If no classes have `openapi.resource: true`, all non-abstract, non-mixin classes with attributes get endpoints.

## Type Mapping

| LinkML Range | OpenAPI Type |
|-------------|-------------|
| `string` | `string` |
| `integer` | `integer` |
| `float` | `number` (format: float) |
| `boolean` | `boolean` |
| `date` | `string` (format: date) |
| `datetime` | `string` (format: date-time) |
| `uri` | `string` (format: uri) |
| Class reference | `$ref` to component schema |
| Enum reference | `$ref` to component schema |
| Multivalued slot | `array` of the above |

## Constraints

LinkML slot constraints map to JSON Schema:

| LinkML | JSON Schema |
|--------|------------|
| `required: true` | In `required` array |
| `pattern` | `pattern` |
| `minimum_value` | `minimum` |
| `maximum_value` | `maximum` |
| `identifier: true` | Path parameter `{id}` |

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
ruff check src/ tests/
ruff format src/ tests/
```
