"""OpenAPI 3.1 generator for LinkML schemas.

Converts LinkML schema definitions into OpenAPI 3.1 specifications with:
- JSON Schema components derived from classes and slots
- CRUD endpoints for classes annotated with openapi.resource: true
- Path/query parameter inference from slot annotations
"""

import json
import re
from typing import Any

import yaml
from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView

# LinkML range → OpenAPI/JSON Schema type mapping
RANGE_TYPE_MAP = {
    "string": {"type": "string"},
    "integer": {"type": "integer"},
    "float": {"type": "number", "format": "float"},
    "double": {"type": "number", "format": "double"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "uri": {"type": "string", "format": "uri"},
    "uriorcurie": {"type": "string", "format": "uri"},
    "decimal": {"type": "number"},
    "ncname": {"type": "string"},
    "nodeidentifier": {"type": "string", "format": "uri"},
}


def _pluralize(name: str) -> str:
    """Simple English pluralization for URL paths."""
    if name.endswith("s") or name.endswith("x") or name.endswith("z"):
        return name + "es"
    if name.endswith("y") and name[-2:] not in ("ay", "ey", "oy", "uy"):
        return name[:-1] + "ies"
    return name + "s"


def _to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return s.lower()


def _to_path_segment(name: str) -> str:
    """Convert class name to URL path segment: CamelCase → snake_case → plural."""
    return _pluralize(_to_snake_case(name))


class OpenAPIGenerator:
    """Generate an OpenAPI 3.1 specification from a LinkML schema."""

    def __init__(
        self,
        schema_view: SchemaView,
        *,
        title: str | None = None,
        version: str = "1.0.0",
        server_url: str = "http://localhost:8000",
        resource_filter: list[str] | None = None,
    ):
        """Initialize the generator.

        Args:
            schema_view: LinkML SchemaView instance
            title: API title (defaults to schema name)
            version: API version string
            server_url: Base server URL
            resource_filter: If provided, only generate endpoints for these class names.
                             If None, generate for classes annotated with openapi.resource: true,
                             or all non-mixin non-abstract classes if none are annotated.
        """
        self.sv = schema_view
        self.schema = schema_view.schema
        self.title = title or str(self.schema.name) or "API"
        self.version = version
        self.server_url = server_url
        self.resource_filter = resource_filter

    def generate(self) -> dict[str, Any]:
        """Generate the complete OpenAPI 3.1 specification."""
        spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {
                "title": self.title,
                "version": self.version,
            },
            "servers": [{"url": self.server_url}],
            "paths": {},
            "components": {"schemas": {}},
        }

        if self.schema.description:
            spec["info"]["description"] = self.schema.description

        # Generate component schemas for all classes
        for class_name in self.sv.all_classes():
            cls = self.sv.get_class(class_name)
            schema_obj = self._class_to_schema(cls)
            spec["components"]["schemas"][class_name] = schema_obj

        # Generate paths for resource classes
        resource_classes = self._get_resource_classes()
        for class_name in resource_classes:
            cls = self.sv.get_class(class_name)
            id_slot = self._get_identifier_slot(cls)
            path_segment = self._get_path_segment(cls)
            operations = self._get_operations(cls)

            # Collection path: /resources
            collection_path = f"/{path_segment}"
            spec["paths"][collection_path] = {}

            if "list" in operations:
                spec["paths"][collection_path]["get"] = self._make_list_operation(cls, class_name)
            if "create" in operations:
                spec["paths"][collection_path]["post"] = self._make_create_operation(
                    cls, class_name
                )

            # Item path: /resources/{id}
            if id_slot:
                id_param_name = id_slot.name
                item_path = f"/{path_segment}/{{{id_param_name}}}"
                spec["paths"][item_path] = {}
                path_param = {
                    "name": id_param_name,
                    "in": "path",
                    "required": True,
                    "schema": self._slot_to_schema(id_slot),
                }
                spec["paths"][item_path]["parameters"] = [path_param]

                if "read" in operations:
                    spec["paths"][item_path]["get"] = self._make_read_operation(cls, class_name)
                if "update" in operations:
                    spec["paths"][item_path]["put"] = self._make_update_operation(cls, class_name)
                if "delete" in operations:
                    spec["paths"][item_path]["delete"] = self._make_delete_operation(
                        cls, class_name
                    )

        # Generate enum schemas
        for enum_name in self.sv.all_enums():
            enum_def = self.sv.get_enum(enum_name)
            spec["components"]["schemas"][enum_name] = self._enum_to_schema(enum_def)

        return spec

    def serialize(self, format: str = "yaml") -> str:
        """Generate and serialize the OpenAPI spec.

        Args:
            format: Output format — "yaml" or "json"
        """
        spec = self.generate()
        # Round-trip through JSON to convert LinkML types to plain Python
        spec = json.loads(json.dumps(spec, default=str))
        if format == "json":
            return json.dumps(spec, indent=2)
        return yaml.dump(spec, default_flow_style=False, sort_keys=False)

    # --- Schema generation ---

    def _class_to_schema(self, cls: ClassDefinition) -> dict[str, Any]:
        """Convert a LinkML class to a JSON Schema object for OpenAPI components."""
        schema: dict[str, Any] = {"type": "object"}

        if cls.description:
            schema["description"] = cls.description

        # Handle inheritance via allOf
        if cls.is_a:
            schema = {
                "allOf": [
                    {"$ref": f"#/components/schemas/{cls.is_a}"},
                    {"type": "object"},
                ]
            }
            if cls.description:
                schema["description"] = cls.description
            props_target = schema["allOf"][1]
        else:
            props_target = schema

        properties = {}
        required = []

        for slot in self.sv.class_induced_slots(cls.name):
            slot_schema = self._slot_to_schema(slot)
            properties[slot.name] = slot_schema

            if slot.required:
                required.append(slot.name)

        if properties:
            props_target["properties"] = properties
        if required:
            props_target["required"] = required

        return schema

    def _slot_to_schema(self, slot: SlotDefinition) -> dict[str, Any]:
        """Convert a LinkML slot to a JSON Schema property."""
        range_name = slot.range or "string"
        schema: dict[str, Any] = {}

        if slot.description:
            schema["description"] = slot.description

        # Check if range is a class or enum
        if self.sv.get_class(range_name):
            ref = {"$ref": f"#/components/schemas/{range_name}"}
            if slot.multivalued:
                schema.update({"type": "array", "items": ref})
            else:
                schema.update(ref)
        elif self.sv.get_enum(range_name):
            ref = {"$ref": f"#/components/schemas/{range_name}"}
            if slot.multivalued:
                schema.update({"type": "array", "items": ref})
            else:
                schema.update(ref)
        else:
            # Primitive type
            type_schema = RANGE_TYPE_MAP.get(range_name, {"type": "string"})
            if slot.multivalued:
                schema.update({"type": "array", "items": type_schema})
            else:
                schema.update(type_schema)

        if slot.pattern:
            schema["pattern"] = slot.pattern
        if slot.minimum_value is not None:
            schema["minimum"] = slot.minimum_value
        if slot.maximum_value is not None:
            schema["maximum"] = slot.maximum_value

        return schema

    def _enum_to_schema(self, enum_def) -> dict[str, Any]:
        """Convert a LinkML enum to a JSON Schema enum."""
        schema: dict[str, Any] = {"type": "string"}
        if enum_def.description:
            schema["description"] = enum_def.description

        values = []
        for pv in enum_def.permissible_values.values():
            values.append(pv.text)
        if values:
            schema["enum"] = values

        return schema

    # --- Resource/path helpers ---

    def _get_resource_classes(self) -> list[str]:
        """Determine which classes should have REST endpoints."""
        if self.resource_filter:
            return self.resource_filter

        # Check for openapi.resource annotation
        annotated = []
        for class_name in self.sv.all_classes():
            cls = self.sv.get_class(class_name)
            annotations = (
                {a.tag: a.value for a in cls.annotations.values()} if cls.annotations else {}
            )
            if annotations.get("openapi.resource") in (True, "true", "True"):
                annotated.append(class_name)

        if annotated:
            return annotated

        # Default: all non-abstract, non-mixin classes with at least one attribute
        return [
            name
            for name in self.sv.all_classes()
            if not self.sv.get_class(name).abstract
            and not self.sv.get_class(name).mixin
            and list(self.sv.class_induced_slots(name))
        ]

    def _get_identifier_slot(self, cls: ClassDefinition) -> SlotDefinition | None:
        """Find the identifier slot for a class."""
        for slot in self.sv.class_induced_slots(cls.name):
            if slot.identifier:
                return slot
        # Fallback: look for a slot named "id"
        for slot in self.sv.class_induced_slots(cls.name):
            if slot.name == "id":
                return slot
        return None

    def _get_path_segment(self, cls: ClassDefinition) -> str:
        """Get the URL path segment for a class."""
        annotations = {a.tag: a.value for a in cls.annotations.values()} if cls.annotations else {}
        if "openapi.path" in annotations:
            return annotations["openapi.path"].lstrip("/")
        return _to_path_segment(cls.name)

    def _get_operations(self, cls: ClassDefinition) -> list[str]:
        """Get the list of CRUD operations for a class."""
        annotations = {a.tag: a.value for a in cls.annotations.values()} if cls.annotations else {}
        if "openapi.operations" in annotations:
            ops = annotations["openapi.operations"]
            if isinstance(ops, str):
                return [o.strip() for o in ops.split(",")]
            return list(ops)
        return ["list", "create", "read", "update", "delete"]

    # --- Operation builders ---

    def _make_list_operation(self, cls: ClassDefinition, class_name: str) -> dict:
        return {
            "summary": f"List {_to_path_segment(class_name).replace('_', ' ')}",
            "operationId": f"list_{_to_path_segment(class_name)}",
            "tags": [class_name],
            "parameters": self._make_query_params(cls),
            "responses": {
                "200": {
                    "description": f"List of {class_name} objects",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "array",
                                "items": {"$ref": f"#/components/schemas/{class_name}"},
                            }
                        }
                    },
                }
            },
        }

    def _make_create_operation(self, cls: ClassDefinition, class_name: str) -> dict:
        return {
            "summary": f"Create a {class_name}",
            "operationId": f"create_{_to_snake_case(class_name)}",
            "tags": [class_name],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": f"#/components/schemas/{class_name}"}}
                },
            },
            "responses": {
                "201": {
                    "description": f"{class_name} created",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{class_name}"}
                        }
                    },
                },
                "422": {"description": "Validation error"},
            },
        }

    def _make_read_operation(self, cls: ClassDefinition, class_name: str) -> dict:
        return {
            "summary": f"Get a {class_name}",
            "operationId": f"get_{_to_snake_case(class_name)}",
            "tags": [class_name],
            "responses": {
                "200": {
                    "description": f"{class_name} details",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{class_name}"}
                        }
                    },
                },
                "404": {"description": "Not found"},
            },
        }

    def _make_update_operation(self, cls: ClassDefinition, class_name: str) -> dict:
        return {
            "summary": f"Update a {class_name}",
            "operationId": f"update_{_to_snake_case(class_name)}",
            "tags": [class_name],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": f"#/components/schemas/{class_name}"}}
                },
            },
            "responses": {
                "200": {
                    "description": f"{class_name} updated",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{class_name}"}
                        }
                    },
                },
                "404": {"description": "Not found"},
                "422": {"description": "Validation error"},
            },
        }

    def _make_delete_operation(self, cls: ClassDefinition, class_name: str) -> dict:
        return {
            "summary": f"Delete a {class_name}",
            "operationId": f"delete_{_to_snake_case(class_name)}",
            "tags": [class_name],
            "responses": {
                "204": {"description": f"{class_name} deleted"},
                "404": {"description": "Not found"},
            },
        }

    def _make_query_params(self, cls: ClassDefinition) -> list[dict]:
        """Generate query parameters for list endpoint filtering."""
        params = [
            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100}},
            {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
        ]

        # Add filterable fields (non-multivalued string/enum/integer slots)
        for slot in self.sv.class_induced_slots(cls.name):
            if not slot.multivalued and not slot.identifier:
                range_name = slot.range or "string"
                if range_name in ("string", "integer", "boolean") or self.sv.get_enum(range_name):
                    params.append(
                        {
                            "name": slot.name,
                            "in": "query",
                            "required": False,
                            "schema": self._slot_to_schema(slot),
                        }
                    )

        return params
