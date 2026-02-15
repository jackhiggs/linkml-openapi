"""OpenAPI 3.1 generator for LinkML schemas.

Converts LinkML schema definitions into OpenAPI 3.1 specifications with:
- JSON Schema components derived from classes and slots
- CRUD endpoints for classes annotated with openapi.resource: true
- Path/query parameter inference from slot annotations
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, ClassVar

import yaml
from linkml.utils.generator import Generator
from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from openapi_pydantic import (
    Components,
    DataType,
    Info,
    MediaType,
    OpenAPI,
    Operation,
    Parameter,
    ParameterLocation,
    PathItem,
    Reference,
    RequestBody,
    Response,
    Schema,
    Server,
)

# LinkML range → OpenAPI DataType mapping
RANGE_TYPE_MAP: dict[str, dict[str, Any]] = {
    "string": {"type": DataType.STRING},
    "integer": {"type": DataType.INTEGER},
    "float": {"type": DataType.NUMBER, "format": "float"},
    "double": {"type": DataType.NUMBER, "format": "double"},
    "boolean": {"type": DataType.BOOLEAN},
    "date": {"type": DataType.STRING, "format": "date"},
    "datetime": {"type": DataType.STRING, "format": "date-time"},
    "uri": {"type": DataType.STRING, "format": "uri"},
    "uriorcurie": {"type": DataType.STRING, "format": "uri"},
    "decimal": {"type": DataType.NUMBER},
    "ncname": {"type": DataType.STRING},
    "nodeidentifier": {"type": DataType.STRING, "format": "uri"},
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


@dataclass
class OpenAPIGenerator(Generator):
    """Generate an OpenAPI 3.1 specification from a LinkML schema."""

    # ClassVar overrides
    generatorname: ClassVar[str] = os.path.basename(__file__)
    generatorversion: ClassVar[str] = "0.1.0"
    valid_formats: ClassVar[list[str]] = ["yaml", "json"]
    uses_schemaloader: ClassVar[bool] = False
    file_extension: ClassVar[str] = "openapi.yaml"

    # Generator-specific options
    api_title: str | None = None
    api_version: str = "1.0.0"
    server_url: str = "http://localhost:8000"
    resource_filter: list[str] | None = None

    def serialize(self, **kwargs) -> str:
        """Generate and serialize the OpenAPI spec."""
        spec = self._build_openapi()
        raw = json.loads(spec.model_dump_json(by_alias=True, exclude_none=True))
        if self.format == "json":
            return json.dumps(raw, indent=2) + "\n"
        return yaml.dump(raw, default_flow_style=False, sort_keys=False)

    def _build_openapi(self) -> OpenAPI:
        """Build the complete OpenAPI model."""
        sv = self.schemaview
        title = self.api_title or str(sv.schema.name) or "API"

        info = Info(title=title, version=self.api_version)
        if sv.schema.description:
            info.description = sv.schema.description

        schemas: dict[str, Schema | Reference] = {}

        # Component schemas for all classes
        for class_name in sv.all_classes():
            cls = sv.get_class(class_name)
            schemas[class_name] = self._class_to_schema(cls)

        # Enum schemas
        for enum_name in sv.all_enums():
            enum_def = sv.get_enum(enum_name)
            schemas[enum_name] = self._enum_to_schema(enum_def)

        # Build paths for resource classes
        paths: dict[str, PathItem] = {}
        for class_name in self._get_resource_classes():
            cls = sv.get_class(class_name)
            path_vars = self._get_path_variables(cls)
            path_segment = self._get_path_segment(cls)
            operations = self._get_operations(cls)

            # Collection path
            collection_path = f"/{path_segment}"
            collection_item = PathItem()
            if "list" in operations:
                collection_item.get = self._make_list_operation(cls, class_name)
            if "create" in operations:
                collection_item.post = self._make_create_operation(cls, class_name)
            paths[collection_path] = collection_item

            # Item path
            if path_vars:
                item_suffix = "/".join(f"{{{s.name}}}" for s in path_vars)
                item_path = f"/{path_segment}/{item_suffix}"
                path_params = [
                    Parameter(
                        name=s.name,
                        param_in=ParameterLocation.PATH,
                        required=True,
                        param_schema=self._slot_to_schema(s),
                    )
                    for s in path_vars
                ]
                item = PathItem(parameters=path_params)
                if "read" in operations:
                    item.get = self._make_read_operation(cls, class_name)
                if "update" in operations:
                    item.put = self._make_update_operation(cls, class_name)
                if "delete" in operations:
                    item.delete = self._make_delete_operation(cls, class_name)
                paths[item_path] = item

        return OpenAPI(
            openapi="3.1.0",
            info=info,
            servers=[Server(url=self.server_url)],
            paths=paths,
            components=Components(schemas=schemas),
        )

    # --- Schema generation ---

    def _class_to_schema(self, cls: ClassDefinition) -> Schema:
        """Convert a LinkML class to a JSON Schema object."""
        sv = self.schemaview

        properties: dict[str, Schema | Reference] = {}
        required: list[str] = []

        for slot in sv.class_induced_slots(cls.name):
            properties[slot.name] = self._slot_to_schema(slot)
            if slot.required:
                required.append(slot.name)

        if cls.is_a:
            local_schema = Schema(type=DataType.OBJECT)
            if properties:
                local_schema.properties = properties
            if required:
                local_schema.required = required

            schema = Schema(
                allOf=[
                    Reference(ref=f"#/components/schemas/{cls.is_a}"),
                    local_schema,
                ]
            )
            if cls.description:
                schema.description = cls.description
            return schema

        schema = Schema(type=DataType.OBJECT)
        if cls.description:
            schema.description = cls.description
        if properties:
            schema.properties = properties
        if required:
            schema.required = required
        return schema

    def _slot_to_schema(self, slot: SlotDefinition) -> Schema | Reference:
        """Convert a LinkML slot to a JSON Schema property."""
        sv = self.schemaview
        range_name = slot.range or "string"

        # Determine the base schema/ref
        if sv.get_class(range_name) or sv.get_enum(range_name):
            ref = Reference(ref=f"#/components/schemas/{range_name}")
            if slot.multivalued:
                base = Schema(type=DataType.ARRAY, items=ref)
            else:
                base = ref
        else:
            type_info = RANGE_TYPE_MAP.get(range_name, {"type": DataType.STRING})
            inner = Schema(**type_info)
            if slot.multivalued:
                base = Schema(type=DataType.ARRAY, items=inner)
            else:
                base = inner

        # Add constraints and description to a Schema wrapper if base is a Reference
        has_extras = (
            slot.description
            or slot.pattern
            or slot.minimum_value is not None
            or slot.maximum_value is not None
        )
        if isinstance(base, Reference) and has_extras:
            # Wrap in allOf to add constraints alongside a $ref
            schema = Schema(allOf=[base])
            if slot.description:
                schema.description = slot.description
            return schema

        if isinstance(base, Schema):
            if slot.description:
                base.description = slot.description
            if slot.pattern:
                base.pattern = slot.pattern
            if slot.minimum_value is not None:
                base.exclusiveMinimum = None
                base.minimum = slot.minimum_value
            if slot.maximum_value is not None:
                base.exclusiveMaximum = None
                base.maximum = slot.maximum_value

        return base

    def _enum_to_schema(self, enum_def) -> Schema:
        """Convert a LinkML enum to a JSON Schema enum."""
        schema = Schema(type=DataType.STRING)
        if enum_def.description:
            schema.description = enum_def.description

        values = [pv.text for pv in enum_def.permissible_values.values()]
        if values:
            schema.enum = values
        return schema

    # --- Resource/path helpers ---

    def _get_resource_classes(self) -> list[str]:
        """Determine which classes should have REST endpoints."""
        sv = self.schemaview

        if self.resource_filter:
            return self.resource_filter

        annotated = []
        for class_name in sv.all_classes():
            cls = sv.get_class(class_name)
            annotations = (
                {a.tag: a.value for a in cls.annotations.values()} if cls.annotations else {}
            )
            if annotations.get("openapi.resource") in (True, "true", "True"):
                annotated.append(class_name)

        if annotated:
            return annotated

        return [
            name
            for name in sv.all_classes()
            if not sv.get_class(name).abstract
            and not sv.get_class(name).mixin
            and list(sv.class_induced_slots(name))
        ]

    def _get_slot_annotation(self, cls: ClassDefinition, slot_name: str, tag: str) -> str | None:
        """Read a slot annotation, checking class slot_usage first, then the slot itself."""
        # Check slot_usage on the class
        if cls.slot_usage:
            for su in (
                cls.slot_usage.values() if isinstance(cls.slot_usage, dict) else cls.slot_usage
            ):
                su_obj = su if not isinstance(su, str) else None
                if su_obj and getattr(su_obj, "name", None) == slot_name:
                    annotations = getattr(su_obj, "annotations", None)
                    if annotations:
                        for ann in (
                            annotations.values() if isinstance(annotations, dict) else [annotations]
                        ):
                            if hasattr(ann, "tag") and ann.tag == tag:
                                return str(ann.value)
        # Check slot's own annotations via schemaview
        sv = self.schemaview
        slot_def = sv.get_slot(slot_name)
        if slot_def and slot_def.annotations:
            for ann in slot_def.annotations.values():
                if ann.tag == tag:
                    return str(ann.value)
        return None

    def _get_path_variables(self, cls: ClassDefinition) -> list[SlotDefinition]:
        """Get path variable slots from annotations, or fall back to identifier."""
        sv = self.schemaview
        annotated = []
        for slot in sv.class_induced_slots(cls.name):
            val = self._get_slot_annotation(cls, slot.name, "openapi.path_variable")
            if val and val.lower() == "true":
                annotated.append(slot)
        if annotated:
            return annotated
        # Fall back to identifier slot
        for slot in sv.class_induced_slots(cls.name):
            if slot.identifier:
                return [slot]
        for slot in sv.class_induced_slots(cls.name):
            if slot.name == "id":
                return [slot]
        return []

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

    def _make_list_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"List {_to_path_segment(class_name).replace('_', ' ')}",
            operationId=f"list_{_to_path_segment(class_name)}",
            tags=[class_name],
            parameters=self._make_query_params(cls),
            responses={
                "200": Response(
                    description=f"List of {class_name} objects",
                    content={
                        "application/json": MediaType(
                            media_type_schema=Schema(
                                type=DataType.ARRAY,
                                items=Reference(ref=f"#/components/schemas/{class_name}"),
                            )
                        )
                    },
                )
            },
        )

    def _make_create_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"Create a {class_name}",
            operationId=f"create_{_to_snake_case(class_name)}",
            tags=[class_name],
            requestBody=RequestBody(
                required=True,
                content={
                    "application/json": MediaType(
                        media_type_schema=Reference(ref=f"#/components/schemas/{class_name}")
                    )
                },
            ),
            responses={
                "201": Response(
                    description=f"{class_name} created",
                    content={
                        "application/json": MediaType(
                            media_type_schema=Reference(ref=f"#/components/schemas/{class_name}")
                        )
                    },
                ),
                "422": Response(description="Validation error"),
            },
        )

    def _make_read_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"Get a {class_name}",
            operationId=f"get_{_to_snake_case(class_name)}",
            tags=[class_name],
            responses={
                "200": Response(
                    description=f"{class_name} details",
                    content={
                        "application/json": MediaType(
                            media_type_schema=Reference(ref=f"#/components/schemas/{class_name}")
                        )
                    },
                ),
                "404": Response(description="Not found"),
            },
        )

    def _make_update_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"Update a {class_name}",
            operationId=f"update_{_to_snake_case(class_name)}",
            tags=[class_name],
            requestBody=RequestBody(
                required=True,
                content={
                    "application/json": MediaType(
                        media_type_schema=Reference(ref=f"#/components/schemas/{class_name}")
                    )
                },
            ),
            responses={
                "200": Response(
                    description=f"{class_name} updated",
                    content={
                        "application/json": MediaType(
                            media_type_schema=Reference(ref=f"#/components/schemas/{class_name}")
                        )
                    },
                ),
                "404": Response(description="Not found"),
                "422": Response(description="Validation error"),
            },
        )

    def _make_delete_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"Delete a {class_name}",
            operationId=f"delete_{_to_snake_case(class_name)}",
            tags=[class_name],
            responses={
                "204": Response(description=f"{class_name} deleted"),
                "404": Response(description="Not found"),
            },
        )

    def _make_query_params(self, cls: ClassDefinition) -> list[Parameter]:
        """Generate query parameters for list endpoint filtering."""
        sv = self.schemaview
        params = [
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

        # Check if any slot has openapi.query_param annotation
        annotated_params = []
        for slot in sv.class_induced_slots(cls.name):
            val = self._get_slot_annotation(cls, slot.name, "openapi.query_param")
            if val and val.lower() == "true":
                annotated_params.append(
                    Parameter(
                        name=slot.name,
                        param_in=ParameterLocation.QUERY,
                        required=False,
                        param_schema=self._slot_to_schema(slot),
                    )
                )

        if annotated_params:
            params.extend(annotated_params)
            return params

        # Fall back to auto-inference
        for slot in sv.class_induced_slots(cls.name):
            if not slot.multivalued and not slot.identifier:
                range_name = slot.range or "string"
                if range_name in ("string", "integer", "boolean") or sv.get_enum(range_name):
                    params.append(
                        Parameter(
                            name=slot.name,
                            param_in=ParameterLocation.QUERY,
                            required=False,
                            param_schema=self._slot_to_schema(slot),
                        )
                    )

        return params
