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

from linkml_openapi import __version__

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


# Class-name suffixes that are already plural (or unchanged in plural form)
# and should be returned as-is from `_pluralize`.
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


def _pluralize(name: str) -> str:
    """Pluralize an English noun for URL paths.

    Handles the common regular-pluralization patterns (`-s/-x/-z/-ch/-sh`,
    consonant-`y`, default `+s`) and the most common already-plural Latin
    forms used in domain modeling (`series`, `species`, `genus`). For
    irregular nouns (`child`, `person`, `index`, …) the function falls
    back to `+s` and the caller is expected to set `openapi.path`
    explicitly when correctness matters; `_warn_on_irregular_plural`
    surfaces a warning at generation time.
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


def _is_irregular_plural_hint(name: str) -> bool:
    """True when `name` looks like it would be misled by the default rules."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(suf) for suf in _IRREGULAR_HINT_SUFFIXES)


def _to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return s.lower()


def _is_truthy(value: object) -> bool:
    """Check if an annotation value represents a boolean true."""
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _to_path_segment(name: str) -> str:
    """Convert class name to URL path segment: CamelCase → snake_case → plural."""
    return _pluralize(_to_snake_case(name))


@dataclass
class OpenAPIGenerator(Generator):
    """Generate an OpenAPI 3.1 specification from a LinkML schema."""

    # ClassVar overrides
    generatorname: ClassVar[str] = os.path.basename(__file__)
    generatorversion: ClassVar[str] = __version__
    valid_formats: ClassVar[list[str]] = ["yaml", "json"]
    uses_schemaloader: ClassVar[bool] = False
    file_extension: ClassVar[str] = "openapi.yaml"

    # Generator-specific options
    api_title: str | None = None
    api_version: str = "1.0.0"
    server_url: str = "http://localhost:8000"
    resource_filter: list[str] | None = None
    # OpenAPI dialect to emit. 3.0.3 is the default because some popular
    # codegens (notably openapi-generator's Spring server library) still
    # mishandle 3.1 + allOf-based inheritance, silently producing duplicate
    # `Foo_1` schemas. Pass "3.1.0" to opt into the newer dialect once
    # downstream tools catch up.
    openapi_version: str = "3.0.3"
    # Inline parent properties directly into subclass schemas instead of
    # using `allOf` + `$ref` for inheritance. Off by default; enable for
    # codegens that still trip on inline-schema-inside-allOf.
    flatten_inheritance: bool = False
    # Emit RFC 7807 Problem (or a user-declared error class) as the body
    # schema for non-2xx responses. Off → today's body-less responses.
    error_schema: bool = True

    def serialize(self, **kwargs) -> str:
        """Generate and serialize the OpenAPI spec."""
        # Reset the x-rdf-* maps; _build_openapi populates them as it walks the schema.
        self._x_rdf_class: dict[str, str] = {}
        self._x_rdf_property: dict[tuple[str, str], str] = {}
        # Resolved name of the schema referenced from non-2xx responses, or
        # None when error_schema is off. Cached per-build so each operation
        # builder doesn't re-resolve.
        self._error_class_name: str | None = self._resolve_error_class()
        spec = self._build_openapi()
        raw = json.loads(spec.model_dump_json(by_alias=True, exclude_none=True))
        raw["openapi"] = self.openapi_version
        self._strip_invalid_parameter_fields(raw)
        self._coerce_numeric_constraints(raw)
        self._inject_rdf_extensions(raw)
        if self.format == "json":
            return json.dumps(raw, indent=2) + "\n"
        return yaml.dump(raw, default_flow_style=False, sort_keys=False)

    @staticmethod
    def _strip_invalid_parameter_fields(spec: dict) -> None:
        """Remove fields that openapi-pydantic defaults but are invalid per OpenAPI 3.1.

        ``allowEmptyValue`` is only valid for query parameters.
        ``allowReserved`` is only valid for query parameters with a schema.
        """
        for path_item in (spec.get("paths") or {}).values():
            for param_list_key in ("parameters",):
                for param in path_item.get(param_list_key) or []:
                    if param.get("in") != "query":
                        param.pop("allowEmptyValue", None)
                        param.pop("allowReserved", None)
            for method in ("get", "put", "post", "delete", "patch", "options", "head", "trace"):
                op = path_item.get(method)
                if not op:
                    continue
                for param in op.get("parameters") or []:
                    if param.get("in") != "query":
                        param.pop("allowEmptyValue", None)
                        param.pop("allowReserved", None)

    @staticmethod
    def _coerce_numeric_constraints(obj: Any) -> None:
        """Convert float-valued constraints to int where the value is whole.

        openapi-pydantic types minimum/maximum as float, but JSON Schema and
        gen-json-schema emit integers when the value has no fractional part.
        """
        if isinstance(obj, dict):
            for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "default"):
                if key in obj and isinstance(obj[key], float) and obj[key] == int(obj[key]):
                    obj[key] = int(obj[key])
            for value in obj.values():
                OpenAPIGenerator._coerce_numeric_constraints(value)
        elif isinstance(obj, list):
            for item in obj:
                OpenAPIGenerator._coerce_numeric_constraints(item)

    def _build_openapi(self) -> OpenAPI:
        """Build the complete OpenAPI model."""
        sv = self.schemaview
        title = self.api_title or str(sv.schema.name) or "API"

        info = Info(title=title, version=self.api_version)
        if sv.schema.description:
            info.description = sv.schema.description

        schemas: dict[str, Schema | Reference] = {}

        # Component schemas for all classes. Slot-walking happens once per
        # class inside `_class_to_schema`; the same walk also records any
        # slot_uri values for `_inject_rdf_extensions` to consume.
        for class_name in sv.all_classes():
            cls = sv.get_class(class_name)
            self._record_rdf_class_uri(cls)
            schemas[class_name] = self._class_to_schema(cls)

        # Enum schemas
        for enum_name in sv.all_enums():
            enum_def = sv.get_enum(enum_name)
            schemas[enum_name] = self._enum_to_schema(enum_def)

        # Synthesise the RFC 7807 Problem schema when no custom error class
        # was declared. Skipped silently if a class literally named "Problem"
        # already exists in the schema.
        if self._error_class_name == "Problem" and "Problem" not in schemas:
            schemas["Problem"] = self._build_problem_schema()

        # Track whether any reference relationship is generated; if so the
        # shared ResourceLink component is added to the schema set below.
        self._needs_resource_link: bool = False

        # Build paths for resource classes
        paths: dict[str, PathItem] = {}
        for class_name in self._get_resource_classes():
            cls = sv.get_class(class_name)
            path_vars = self._get_path_variables(cls)
            path_segment = self._get_path_segment(cls)
            operations = self._get_operations(cls)

            self._validate_resource_addressability(class_name, path_vars, operations)

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
                item_suffix = "/".join(f"{{{s.name}}}" for s, _mode in path_vars)
                item_path = f"/{path_segment}/{item_suffix}"
                path_params = [
                    Parameter(
                        name=s.name,
                        param_in=ParameterLocation.PATH,
                        required=True,
                        param_schema=self._path_variable_schema(s, mode),
                    )
                    for s, mode in path_vars
                ]
                item = PathItem(parameters=path_params)
                if "read" in operations:
                    item.get = self._make_read_operation(cls, class_name)
                if "update" in operations:
                    item.put = self._make_update_operation(cls, class_name)
                if "delete" in operations:
                    item.delete = self._make_delete_operation(cls, class_name)
                paths[item_path] = item

                paths.update(self._make_nested_paths(class_name, path_segment, path_vars))

        if self._needs_resource_link and "ResourceLink" not in schemas:
            schemas["ResourceLink"] = self._build_resource_link_schema()

        # openapi-pydantic restricts the `openapi` field to 3.1.x literals;
        # we build the model with 3.1.0 and then rewrite the version string
        # to self.openapi_version in serialize(). The 3.0.3 / 3.1.0 spec
        # bodies this generator emits are otherwise structurally identical.
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

        if cls.is_a and not self.flatten_inheritance:
            parent_slot_names = {s.name for s in sv.class_induced_slots(cls.is_a)}
            local_properties: dict[str, Schema | Reference] = {}
            local_required: list[str] = []
            for slot in sv.class_induced_slots(cls.name):
                self._record_rdf_slot_uri(cls.name, slot)
                if slot.name not in parent_slot_names:
                    local_properties[slot.name] = self._slot_to_schema(slot)
                    if slot.required:
                        local_required.append(slot.name)

            local_schema = Schema(type=DataType.OBJECT, additionalProperties=False)
            if local_properties:
                local_schema.properties = local_properties
            if local_required:
                local_schema.required = local_required

            schema = Schema(
                allOf=[
                    Reference(ref=f"#/components/schemas/{cls.is_a}"),
                    local_schema,
                ]
            )
            schema.title = cls.name
            if cls.description:
                schema.description = cls.description
            return schema

        # Flat schema: every induced slot (inherited and local) as a
        # top-level property. Used for non-inheriting classes and when
        # `flatten_inheritance` is on.
        properties: dict[str, Schema | Reference] = {}
        required: list[str] = []

        for slot in sv.class_induced_slots(cls.name):
            self._record_rdf_slot_uri(cls.name, slot)
            properties[slot.name] = self._slot_to_schema(slot)
            if slot.required:
                required.append(slot.name)

        schema = Schema(type=DataType.OBJECT, additionalProperties=False)
        schema.title = cls.name
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

        # `openapi.format` overrides whatever format the range heuristics
        # picked. For multivalued slots the format applies to the array
        # `items`, not the array itself (which has no format).
        format_override = self._slot_format_override(slot)
        if format_override:
            target = base
            if (
                isinstance(base, Schema)
                and base.type == DataType.ARRAY
                and isinstance(base.items, Schema)
            ):
                target = base.items
            if isinstance(target, Schema):
                target.schema_format = format_override

        return base

    @staticmethod
    def _slot_format_override(slot: SlotDefinition) -> str | None:
        """Read openapi.format from a slot's own annotations."""
        annotations = getattr(slot, "annotations", None)
        if not annotations:
            return None
        # For inline `attributes:`-style slots `annotations` arrives as a
        # jsonasobj2 JsonObj rather than a dict — it has no .values() but
        # is still keyed-iterable.
        if isinstance(annotations, dict):
            ann_values: list = list(annotations.values())
        else:
            ann_values = [annotations[k] for k in annotations]
        for ann in ann_values:
            if getattr(ann, "tag", None) == "openapi.format":
                return str(ann.value)
        return None

    def _path_variable_schema(self, slot: SlotDefinition, mode: str) -> Schema | Reference:
        """Pick the parameter schema for a path variable, honouring the mode.

        In "slug" mode the parameter is a plain string regardless of the
        slot's range — this matches the URL-segment-as-slug convention,
        where the body still carries the full IRI in the same field.
        In "iri" mode the slot's full schema is used, preserving any
        `format: uri` typing.
        """
        if mode == "slug":
            schema = Schema(type=DataType.STRING)
            if slot.description:
                schema.description = slot.description
            return schema
        return self._slot_to_schema(slot)

    def _enum_to_schema(self, enum_def) -> Schema:
        """Convert a LinkML enum to a JSON Schema enum."""
        schema = Schema(type=DataType.STRING)
        schema.title = enum_def.name
        if enum_def.description:
            schema.description = enum_def.description

        values = [pv.text for pv in enum_def.permissible_values.values()]
        if values:
            schema.enum = values
        return schema

    # --- Resource/path helpers ---

    @staticmethod
    def _class_annotation(cls: ClassDefinition, tag: str) -> str | None:
        """Read a single class-level annotation value, or None if absent."""
        if not cls.annotations:
            return None
        for ann in cls.annotations.values():
            if ann.tag == tag:
                return str(ann.value)
        return None

    def _get_resource_classes(self) -> list[str]:
        """Determine which classes should have REST endpoints."""
        sv = self.schemaview

        if self.resource_filter:
            return self.resource_filter

        annotated = [
            name
            for name in sv.all_classes()
            if _is_truthy(self._class_annotation(sv.get_class(name), "openapi.resource") or False)
        ]
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

    @staticmethod
    def _path_variable_mode(value: str | None) -> str | None:
        """Normalize openapi.path_variable values.

        Accepted forms:
            "true" / "iri" — preserve the slot's range typing on the path parameter
                             (e.g. `string format=uri` for `range: uri`)
            "slug"          — emit `string` regardless of slot range; useful when the
                             URL segment is a slug derived from the resource's IRI,
                             not the IRI itself
            anything else   — not a path variable

        Returns "iri" or "slug" when the slot is a path variable, else None.
        """
        if value is None:
            return None
        v = str(value).strip().lower()
        if v in ("true", "iri"):
            return "iri"
        if v == "slug":
            return "slug"
        return None

    def _get_path_variables(self, cls: ClassDefinition) -> list[tuple[SlotDefinition, str]]:
        """Get path variable slots and their mode from annotations.

        Returns a list of (slot, mode) where mode is "slug" or "iri".
        Falls back to the identifier slot (or one literally named "id")
        in "iri" mode when no slots are explicitly annotated.
        """
        annotated: list[tuple[SlotDefinition, str]] = []
        identifier_slot: SlotDefinition | None = None
        id_named_slot: SlotDefinition | None = None
        for slot in self.schemaview.class_induced_slots(cls.name):
            mode = self._path_variable_mode(
                self._get_slot_annotation(cls, slot.name, "openapi.path_variable")
            )
            if mode:
                annotated.append((slot, mode))
            if identifier_slot is None and slot.identifier:
                identifier_slot = slot
            if id_named_slot is None and slot.name == "id":
                id_named_slot = slot
        if annotated:
            return annotated
        fallback = identifier_slot or id_named_slot
        return [(fallback, "iri")] if fallback else []

    def _get_path_segment(self, cls: ClassDefinition) -> str:
        """Get the URL path segment for a class."""
        explicit = self._class_annotation(cls, "openapi.path")
        if explicit is not None:
            return explicit.lstrip("/")
        if _is_irregular_plural_hint(cls.name):
            import warnings

            warnings.warn(
                f"Class {cls.name!r} has an irregular English plural that the "
                "default pluralizer cannot handle correctly. Set "
                "`openapi.path:` on the class to fix the URL.",
                stacklevel=2,
            )
        return _to_path_segment(cls.name)

    def _get_operations(self, cls: ClassDefinition) -> list[str]:
        """Get the list of CRUD operations for a class."""
        ops = self._class_annotation(cls, "openapi.operations")
        if ops is None:
            return ["list", "create", "read", "update", "delete"]
        return [o.strip() for o in ops.split(",")]

    # --- RDF extension propagation -----------------------------------------

    def _record_rdf_class_uri(self, cls: ClassDefinition) -> None:
        """Remember the class's expanded class_uri for later injection.

        Slot-level URIs are recorded in the same pass as schema generation
        in `_class_to_schema`, so we don't need a parallel slot walk here.
        """
        if cls.class_uri:
            self._x_rdf_class[cls.name] = self.schemaview.expand_curie(cls.class_uri)

    def _record_rdf_slot_uri(self, class_name: str, slot: SlotDefinition) -> None:
        """Remember a slot's expanded slot_uri keyed by (class, slot)."""
        if slot.slot_uri:
            self._x_rdf_property[(class_name, slot.name)] = self.schemaview.expand_curie(
                slot.slot_uri
            )

    def _inject_rdf_extensions(self, raw: dict) -> None:
        """Walk the dumped spec and decorate schemas with x-rdf-* extensions."""
        schemas = (raw.get("components") or {}).get("schemas") or {}
        for class_name, schema in schemas.items():
            class_uri = self._x_rdf_class.get(class_name)
            if class_uri:
                schema["x-rdf-class"] = class_uri
            # Properties may live at top level or inside the inline schema half
            # of an `allOf` (used for inheritance).
            holders: list[dict] = []
            if isinstance(schema.get("properties"), dict):
                holders.append(schema["properties"])
            for sub in schema.get("allOf") or []:
                if isinstance(sub, dict) and isinstance(sub.get("properties"), dict):
                    holders.append(sub["properties"])
            for props in holders:
                for slot_name, slot_schema in props.items():
                    slot_uri = self._x_rdf_property.get((class_name, slot_name))
                    if slot_uri and isinstance(slot_schema, dict):
                        slot_schema["x-rdf-property"] = slot_uri

    # --- Operation builders ------------------------------------------------

    def _content_for(
        self, schema: Schema | Reference, media_types: list[str]
    ) -> dict[str, MediaType]:
        """Build a `content` dict advertising the same schema under every media type."""
        return {mt: MediaType(media_type_schema=schema) for mt in media_types}

    def _get_media_types(self, cls: ClassDefinition) -> list[str]:
        """Read the openapi.media_types class annotation, defaulting to JSON only."""
        raw = self._class_annotation(cls, "openapi.media_types")
        if not raw:
            return ["application/json"]
        return [m.strip() for m in raw.split(",") if m.strip()]

    # --- Error model (RFC 7807) -------------------------------------------

    def _schema_annotation(self, tag: str) -> str | None:
        """Read a top-level schema annotation."""
        annotations = getattr(self.schemaview.schema, "annotations", None)
        if not annotations:
            return None
        for ann in annotations.values():
            if ann.tag == tag:
                return str(ann.value)
        return None

    def _resolve_error_class(self) -> str | None:
        """Pick the schema referenced from non-2xx response bodies, or None.

        With ``error_schema`` off, returns None and no error body is emitted.
        With it on, honours a ``openapi.error_class`` schema annotation if
        present (and validates that the named class exists), otherwise
        falls back to ``"Problem"`` — synthesised in `_build_openapi`.
        """
        if not self.error_schema:
            return None
        custom = self._schema_annotation("openapi.error_class")
        if custom is None:
            return "Problem"
        if self.schemaview.get_class(custom) is None:
            raise ValueError(
                f"openapi.error_class refers to undefined class {custom!r}; "
                "add the class to the schema or remove the annotation."
            )
        return custom

    @staticmethod
    def _build_problem_schema() -> Schema:
        """Construct the RFC 7807 Problem Details component schema."""
        schema = Schema(type=DataType.OBJECT, additionalProperties=True)
        schema.title = "Problem"
        schema.description = (
            "RFC 7807 Problem Details for HTTP APIs. Default error response "
            "shape for non-2xx replies; additional members are permitted "
            "per RFC 7807 §3.2."
        )
        schema.properties = {
            "type": Schema(
                type=DataType.STRING,
                format="uri",
                default="about:blank",
                description="A URI reference identifying the problem type.",
            ),
            "title": Schema(
                type=DataType.STRING,
                description="A short, human-readable summary of the problem type.",
            ),
            "status": Schema(
                type=DataType.INTEGER,
                format="int32",
                description="The HTTP status code generated by the origin server.",
            ),
            "detail": Schema(
                type=DataType.STRING,
                description="A human-readable explanation specific to this occurrence.",
            ),
            "instance": Schema(
                type=DataType.STRING,
                format="uri",
                description="A URI reference identifying this specific occurrence.",
            ),
        }
        return schema

    def _error_response(self, description: str) -> Response:
        """Build a non-2xx Response, attaching the error body when enabled."""
        if not self._error_class_name:
            return Response(description=description)
        ref = Reference(ref=f"#/components/schemas/{self._error_class_name}")
        return Response(
            description=description,
            content={
                "application/json": MediaType(media_type_schema=ref),
                "application/problem+json": MediaType(media_type_schema=ref),
            },
        )

    def _make_list_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        media_types = self._get_media_types(cls)
        array_schema = Schema(
            type=DataType.ARRAY,
            items=Reference(ref=f"#/components/schemas/{class_name}"),
        )
        return Operation(
            summary=f"List {_to_path_segment(class_name).replace('_', ' ')}",
            operationId=f"list_{_to_path_segment(class_name)}",
            tags=[class_name],
            parameters=self._make_query_params(cls),
            responses={
                "200": Response(
                    description=f"List of {class_name} objects",
                    content=self._content_for(array_schema, media_types),
                )
            },
        )

    def _make_create_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        media_types = self._get_media_types(cls)
        ref = Reference(ref=f"#/components/schemas/{class_name}")
        return Operation(
            summary=f"Create a {class_name}",
            operationId=f"create_{_to_snake_case(class_name)}",
            tags=[class_name],
            requestBody=RequestBody(
                required=True,
                content=self._content_for(ref, media_types),
            ),
            responses={
                "201": Response(
                    description=f"{class_name} created",
                    content=self._content_for(ref, media_types),
                ),
                "422": self._error_response("Validation error"),
            },
        )

    def _make_read_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        media_types = self._get_media_types(cls)
        ref = Reference(ref=f"#/components/schemas/{class_name}")
        return Operation(
            summary=f"Get a {class_name}",
            operationId=f"get_{_to_snake_case(class_name)}",
            tags=[class_name],
            responses={
                "200": Response(
                    description=f"{class_name} details",
                    content=self._content_for(ref, media_types),
                ),
                "404": self._error_response("Not found"),
            },
        )

    def _make_update_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        media_types = self._get_media_types(cls)
        ref = Reference(ref=f"#/components/schemas/{class_name}")
        return Operation(
            summary=f"Update a {class_name}",
            operationId=f"update_{_to_snake_case(class_name)}",
            tags=[class_name],
            requestBody=RequestBody(
                required=True,
                content=self._content_for(ref, media_types),
            ),
            responses={
                "200": Response(
                    description=f"{class_name} updated",
                    content=self._content_for(ref, media_types),
                ),
                "404": self._error_response("Not found"),
                "422": self._error_response("Validation error"),
            },
        )

    def _make_delete_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        return Operation(
            summary=f"Delete a {class_name}",
            operationId=f"delete_{_to_snake_case(class_name)}",
            tags=[class_name],
            responses={
                "204": Response(description=f"{class_name} deleted"),
                "404": self._error_response("Not found"),
            },
        )

    # --- Composition vs reference (issue #18) ------------------------------

    def _validate_resource_addressability(
        self, class_name: str, path_vars: list, operations: list[str]
    ) -> None:
        """Resource classes that need item-path operations must be addressable.

        Item-path ops (read / update / delete) require either an identifier
        slot or an explicit ``openapi.path_variable`` annotation. If neither
        is present the class can't be referenced individually and the
        generator should fail loudly rather than silently drop the item path.
        """
        item_ops = {"read", "update", "delete"} & set(operations)
        if item_ops and not path_vars:
            raise ValueError(
                f'Class {class_name!r} has openapi.resource: "true" with '
                f"item-path operations ({sorted(item_ops)}) but no identifier "
                "slot and no `openapi.path_variable` annotation. Either add "
                "an identifier slot, mark a slot as path_variable, or limit "
                "openapi.operations to list/create."
            )

    def _identifier_slot(self, class_name: str) -> SlotDefinition | None:
        """Return the identifier slot of the class, or None if it has none."""
        for slot in self.schemaview.class_induced_slots(class_name):
            if slot.identifier:
                return slot
        return None

    @staticmethod
    def _is_composition(slot: SlotDefinition) -> bool:
        """True when a class-ranged slot is composition rather than reference.

        Reads LinkML's `inlined` flag, which SchemaView already resolves to
        the right default: True when the target class has no identifier
        (composition is the only option), False when it does (reference).
        """
        return bool(slot.inlined)

    @staticmethod
    def _build_resource_link_schema() -> Schema:
        """The shared body schema for reference attach operations."""
        schema = Schema(type=DataType.OBJECT)
        schema.title = "ResourceLink"
        schema.description = (
            "A reference to another resource by IRI. Body shape for attach "
            "operations on reference relationships."
        )
        schema.required = ["id"]
        schema.properties = {
            "id": Schema(
                type=DataType.STRING,
                format="uri",
                description="IRI of the linked resource.",
            ),
        }
        return schema

    @staticmethod
    def _nested_item_path_var(target_class_name: str) -> str:
        """Path-variable name for the linked item under a nested path.

        Avoids colliding with the parent's ``{id}`` by namespacing on the
        target class (e.g. ``/books/{id}/authors/{author_id}``).
        """
        return f"{_to_snake_case(target_class_name)}_id"

    def _make_nested_paths(
        self,
        parent_class_name: str,
        parent_path_segment: str,
        parent_path_vars: list,
    ) -> dict[str, PathItem]:
        """Generate nested paths for composition and reference relationships.

        Walks every multivalued, class-ranged slot on the parent and emits:

        - **Composition** (``slot.inlined``): full CRUD nested at
          ``/{parent}/{id}/{slot}`` (and ``/{slot}/{target_id}`` when the
          target has an identifier).
        - **Reference** (target has identifier, ``inlined: false``): GET to
          list, POST with a ``ResourceLink`` body to attach (single or batch
          via ``oneOf``), DELETE on the per-target item path to detach.
        """
        sv = self.schemaview
        out: dict[str, PathItem] = {}
        parent_var_suffix = "/".join(f"{{{s.name}}}" for s, _mode in parent_path_vars)
        parent_path_params = [
            Parameter(
                name=s.name,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._path_variable_schema(s, mode),
            )
            for s, mode in parent_path_vars
        ]

        for slot in sv.class_induced_slots(parent_class_name):
            if not slot.multivalued:
                continue
            target_name = slot.range
            if not target_name or sv.get_class(target_name) is None:
                continue  # primitive, enum, or unresolved range

            collection_path = f"/{parent_path_segment}/{parent_var_suffix}/{slot.name}"
            target_id_slot = self._identifier_slot(target_name)

            if self._is_composition(slot):
                self._add_composition_paths(
                    out,
                    collection_path,
                    parent_class_name,
                    slot,
                    target_name,
                    target_id_slot,
                    parent_path_params,
                )
            else:
                self._needs_resource_link = True
                self._add_reference_paths(
                    out,
                    collection_path,
                    parent_class_name,
                    slot,
                    target_name,
                    target_id_slot,
                    parent_path_params,
                )

        return out

    def _add_composition_paths(
        self,
        paths: dict,
        collection_path: str,
        parent_class_name: str,
        slot: SlotDefinition,
        target_class_name: str,
        target_id_slot: SlotDefinition | None,
        parent_path_params: list[Parameter],
    ) -> None:
        target_cls = self.schemaview.get_class(target_class_name)
        media_types = self._get_media_types(target_cls)
        target_ref = Reference(ref=f"#/components/schemas/{target_class_name}")
        array_schema = Schema(type=DataType.ARRAY, items=target_ref)
        op_tag = f"{parent_class_name}.{slot.name}"
        slot_seg = slot.name

        collection = PathItem(parameters=list(parent_path_params))
        collection.get = Operation(
            summary=f"List {target_class_name} composed in {parent_class_name}.{slot.name}",
            operationId=f"list_{_to_snake_case(parent_class_name)}_{slot_seg}",
            tags=[op_tag],
            responses={
                "200": Response(
                    description=f"{target_class_name} list",
                    content=self._content_for(array_schema, media_types),
                ),
                "404": self._error_response("Parent not found"),
            },
        )
        collection.post = Operation(
            summary=f"Create a {target_class_name} in {parent_class_name}.{slot.name}",
            operationId=f"create_{_to_snake_case(parent_class_name)}_{slot_seg}",
            tags=[op_tag],
            requestBody=RequestBody(
                required=True, content=self._content_for(target_ref, media_types)
            ),
            responses={
                "201": Response(
                    description=f"{target_class_name} created",
                    content=self._content_for(target_ref, media_types),
                ),
                "404": self._error_response("Parent not found"),
                "422": self._error_response("Validation error"),
            },
        )
        paths[collection_path] = collection

        # Item path is only emitted when the target has an identifier — a
        # composed child without one has no addressable handle.
        if target_id_slot is None:
            return

        item_var = self._nested_item_path_var(target_class_name)
        item_path = f"{collection_path}/{{{item_var}}}"
        item_path_params = list(parent_path_params) + [
            Parameter(
                name=item_var,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._slot_to_schema(target_id_slot),
            )
        ]
        item = PathItem(parameters=item_path_params)
        item.get = Operation(
            summary=f"Get a {target_class_name} from {parent_class_name}.{slot.name}",
            operationId=f"get_{_to_snake_case(parent_class_name)}_{slot_seg}_item",
            tags=[op_tag],
            responses={
                "200": Response(
                    description=f"{target_class_name} details",
                    content=self._content_for(target_ref, media_types),
                ),
                "404": self._error_response("Not found"),
            },
        )
        item.put = Operation(
            summary=f"Replace a {target_class_name} in {parent_class_name}.{slot.name}",
            operationId=f"replace_{_to_snake_case(parent_class_name)}_{slot_seg}_item",
            tags=[op_tag],
            requestBody=RequestBody(
                required=True, content=self._content_for(target_ref, media_types)
            ),
            responses={
                "200": Response(
                    description=f"{target_class_name} replaced",
                    content=self._content_for(target_ref, media_types),
                ),
                "404": self._error_response("Not found"),
                "422": self._error_response("Validation error"),
            },
        )
        item.delete = Operation(
            summary=f"Delete a {target_class_name} from {parent_class_name}.{slot.name}",
            operationId=f"delete_{_to_snake_case(parent_class_name)}_{slot_seg}_item",
            tags=[op_tag],
            responses={
                "204": Response(description=f"{target_class_name} deleted"),
                "404": self._error_response("Not found"),
            },
        )
        paths[item_path] = item

    def _add_reference_paths(
        self,
        paths: dict,
        collection_path: str,
        parent_class_name: str,
        slot: SlotDefinition,
        target_class_name: str,
        target_id_slot: SlotDefinition | None,
        parent_path_params: list[Parameter],
    ) -> None:
        # Reference relationships require the target to be addressable —
        # otherwise there's no IRI to put in the ResourceLink body.
        if target_id_slot is None:
            raise ValueError(
                f"Slot {parent_class_name}.{slot.name!r} (range "
                f"{target_class_name!r}) is non-inlined but the target has "
                "no identifier slot. Either mark the slot `inlined: true` "
                "or add an identifier to the target class."
            )

        target_cls = self.schemaview.get_class(target_class_name)
        media_types = self._get_media_types(target_cls)
        target_ref = Reference(ref=f"#/components/schemas/{target_class_name}")
        array_schema = Schema(type=DataType.ARRAY, items=target_ref)

        link_ref = Reference(ref="#/components/schemas/ResourceLink")
        # Body accepts a single link or a batch — clients prefer batch.
        link_body_schema = Schema(oneOf=[link_ref, Schema(type=DataType.ARRAY, items=link_ref)])
        op_tag = f"{parent_class_name}.{slot.name}"
        slot_seg = slot.name

        collection = PathItem(parameters=list(parent_path_params))
        collection.get = Operation(
            summary=f"List {target_class_name} attached to {parent_class_name}.{slot.name}",
            operationId=f"list_{_to_snake_case(parent_class_name)}_{slot_seg}",
            tags=[op_tag],
            responses={
                "200": Response(
                    description=f"{target_class_name} list",
                    content=self._content_for(array_schema, media_types),
                ),
                "404": self._error_response("Parent not found"),
            },
        )
        collection.post = Operation(
            summary=f"Attach {target_class_name} to {parent_class_name}.{slot.name}",
            operationId=f"attach_{_to_snake_case(parent_class_name)}_{slot_seg}",
            tags=[op_tag],
            requestBody=RequestBody(
                required=True,
                content={"application/json": MediaType(media_type_schema=link_body_schema)},
            ),
            responses={
                "204": Response(description="Attached"),
                "404": self._error_response("Parent or target not found"),
                "422": self._error_response("Validation error"),
            },
        )
        paths[collection_path] = collection

        item_var = self._nested_item_path_var(target_class_name)
        item_path = f"{collection_path}/{{{item_var}}}"
        item_path_params = list(parent_path_params) + [
            Parameter(
                name=item_var,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._slot_to_schema(target_id_slot),
            )
        ]
        item = PathItem(parameters=item_path_params)
        item.delete = Operation(
            summary=f"Detach {target_class_name} from {parent_class_name}.{slot.name}",
            operationId=f"detach_{_to_snake_case(parent_class_name)}_{slot_seg}",
            tags=[op_tag],
            responses={
                "204": Response(description="Detached (target entity preserved)"),
                "404": self._error_response("Parent or attachment not found"),
            },
        )
        paths[item_path] = item

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
            if val and _is_truthy(val):
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
