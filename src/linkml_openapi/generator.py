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
from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from openapi_pydantic import (
    Components,
    DataType,
    Discriminator,
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
from linkml_openapi._base import Generator

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


def _parse_csv(value: str | None, *, lowercase: bool = False) -> list[str]:
    """Split a comma-separated annotation value, trimming whitespace and empties."""
    if not value:
        return []
    out = [t.strip() for t in str(value).split(",")]
    if lowercase:
        out = [t.lower() for t in out]
    return [t for t in out if t]


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
    # schema for non-2xx responses. Off emits description-only responses.
    error_schema: bool = True
    # Active profile name (or None for the full surface). Profiles are
    # declared via `openapi.profile.<name>.<key>` schema annotations; a
    # profile lets a single LinkML schema drive multiple API surfaces
    # (internal / partner / external) by excluding classes or slots.
    profile: str | None = None

    def serialize(self, **kwargs) -> str:
        """Generate and serialize the OpenAPI spec."""
        # Reset the x-rdf-* maps; _build_openapi populates them as it walks the schema.
        self._x_rdf_class: dict[str, str] = {}
        self._x_rdf_property: dict[tuple[str, str], str] = {}
        # Resolved name of the schema referenced from non-2xx responses, or
        # None when error_schema is off. Cached per-build so each operation
        # builder doesn't re-resolve.
        self._error_class_name: str | None = self._resolve_error_class()
        # Resolve the active profile (or no-op when self.profile is None).
        # `_resolve_profile_filter` also runs drift detection — failing
        # loudly if an excluded slot is referenced by another annotation.
        (
            self._excluded_classes,
            self._excluded_slots,
            self._profile_description,
        ) = self._resolve_profile_filter()
        # Pre-compute the synthetic-inverse index once; without this each
        # resource-class iteration in `_build_openapi` would re-walk every
        # class and slot in the schema (O(resource_classes × all_classes ×
        # max_slots) per build).
        self._synthetic_inverses_index = self._collect_synthetic_inverses()
        # Pre-compute the parent-chain index so each resource-class
        # iteration in `_build_openapi` can ask for its canonical chain in
        # O(1) instead of re-walking the relationship graph.
        self._parent_chains_index = self._collect_parent_chains()
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
        if self.profile:
            tag_line = (
                f"\n\n[Profile: {self.profile}] {self._profile_description}"
                if self._profile_description
                else f"\n\n[Profile: {self.profile}]"
            )
            info.description = (info.description or "") + tag_line

        schemas: dict[str, Schema | Reference] = {}

        # Component schemas for all classes (filtered by the active profile).
        # Slot-walking happens once per class inside `_class_to_schema`; the
        # same walk also records any slot_uri values for
        # `_inject_rdf_extensions` to consume.
        for class_name in sv.all_classes():
            if class_name in self._excluded_classes:
                continue
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

        # Apply discriminator blocks driven by `designates_type` or by
        # `openapi.discriminator` annotations. Done after class schemas are
        # built so we can patch the in-memory Schema models in place.
        self._apply_discriminators(schemas)

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
            nested_only = _is_truthy(self._class_annotation(cls, "openapi.nested_only") or False)

            self._validate_resource_addressability(class_name, path_vars, operations)

            # Collection path. Suppressed when `openapi.nested_only: "true"`
            # makes the deep-nested URL the only canonical surface.
            if not nested_only:
                collection_path = f"/{path_segment}"
                collection_item = PathItem()
                if "list" in operations:
                    collection_item.get = self._make_list_operation(cls, class_name)
                if "create" in operations:
                    collection_item.post = self._make_create_operation(cls, class_name)
                paths[collection_path] = collection_item

            # Item path. When the class declares `openapi.path_id` *and*
            # has exactly one path variable, the override renames the URL
            # parameter (and the matching `Parameter.name`) so the same
            # identifier shows up consistently in the flat item path, in
            # nested-paths-from-this-class, and in any deep chain that
            # passes through this class as an ancestor.
            if path_vars:
                path_id_override = (
                    self._class_annotation(cls, "openapi.path_id") if len(path_vars) == 1 else None
                )
                resolved_path_vars = [
                    (
                        path_id_override.strip() if path_id_override else slot.name,
                        slot,
                        mode,
                    )
                    for slot, mode in path_vars
                ]
                item_suffix = "/".join(f"{{{name}}}" for name, _slot, _mode in resolved_path_vars)
                path_params = [
                    Parameter(
                        name=name,
                        param_in=ParameterLocation.PATH,
                        required=True,
                        param_schema=self._path_variable_schema(slot, mode),
                    )
                    for name, slot, mode in resolved_path_vars
                ]

                if not nested_only:
                    item_path = f"/{path_segment}/{item_suffix}"
                    item = PathItem(parameters=path_params)
                    if "read" in operations:
                        item.get = self._make_read_operation(cls, class_name)
                    if "update" in operations:
                        item.put = self._make_update_operation(cls, class_name)
                    if "patch" in operations:
                        item.patch = self._make_patch_operation(cls, class_name)
                    if "delete" in operations:
                        item.delete = self._make_delete_operation(cls, class_name)
                    paths[item_path] = item

                # The PATCH body schema is needed whenever `patch` is
                # listed, regardless of which URL forms emit, so generate
                # it outside the nested-only branch.
                if "patch" in operations and f"{class_name}Patch" not in schemas:
                    schemas[f"{class_name}Patch"] = self._build_patch_schema(class_name, cls)

                paths.update(self._make_nested_paths(class_name, path_segment, path_vars))

                # Deep nested paths: when this class has a canonical parent
                # chain, also emit the leaf's item path under the chain
                # prefix and its children under that path. Each ancestor
                # contributes a path-parameter sourced from its identifier
                # slot — *not* from any field on this class — so the leaf
                # component schema stays unchanged.
                chain = self._canonical_parent_chain(class_name)
                if chain:
                    # `chain_prefix` already ends in the slot name that
                    # leads to the leaf class (e.g.
                    # ``catalogs/{catalogId}/datasets/{datasetId}/distributions``).
                    # The deep item URL just appends the leaf's identifier
                    # variable — no extra ``/<leaf_segment>`` segment, since
                    # the slot already carries the noun.
                    #
                    # We only emit the leaf's *own* deep item path here.
                    # Deeper children (Distribution under
                    # /catalogs/.../datasets/.../distributions) are handled
                    # naturally as those children's own canonical chain —
                    # so each chain depth gets emitted exactly once,
                    # without duplicating paths or risking cycles when
                    # synthetic inverses fold a relationship back through
                    # the chain.
                    chain_prefix, chain_params = self._build_chain_path_params(chain)
                    deep_item_path = f"/{chain_prefix}/{item_suffix}"
                    deep_item_params = list(chain_params) + path_params
                    deep_item = PathItem(parameters=deep_item_params)
                    if "read" in operations:
                        deep_item.get = self._make_read_operation(cls, class_name)
                    if "update" in operations:
                        deep_item.put = self._make_update_operation(cls, class_name)
                    if "patch" in operations:
                        deep_item.patch = self._make_patch_operation(cls, class_name)
                    if "delete" in operations:
                        deep_item.delete = self._make_delete_operation(cls, class_name)

                    # OpenAPI requires globally unique `operationId` values.
                    # The deep item's CRUD operations share IDs with the
                    # leaf's flat item path. Append a chain-derived suffix
                    # so each deep operation gets a unique, deterministic
                    # ID without touching the flat-path operations.
                    chain_suffix = "_via_" + "_".join(_to_snake_case(p) for p, _ in chain)
                    deep_paths = {deep_item_path: deep_item}
                    self._suffix_operation_ids(deep_paths, chain_suffix)
                    paths.update(deep_paths)

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
                if self._is_slot_excluded(slot):
                    continue
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
            if self._is_slot_excluded(slot):
                continue
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
        """Determine which classes should have REST endpoints.

        Profile filtering applies last: a class excluded by the active
        profile never gets endpoints, even if it carries
        ``openapi.resource: "true"``.
        """
        sv = self.schemaview
        excluded = self._excluded_classes

        if self.resource_filter:
            return [c for c in self.resource_filter if c not in excluded]

        annotated = [
            name
            for name in sv.all_classes()
            if name not in excluded
            and _is_truthy(self._class_annotation(sv.get_class(name), "openapi.resource") or False)
        ]
        if annotated:
            return annotated

        return [
            name
            for name in sv.all_classes()
            if name not in excluded
            and not sv.get_class(name).abstract
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
        return _parse_csv(ops)

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
        return _parse_csv(raw)

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

    # --- Profiles (issue #17) ----------------------------------------------

    _PROFILE_LIST_KEYS = ("exclude_classes", "exclude_slots", "include_classes", "include_slots")
    _PROFILE_STR_KEYS = ("description",)

    def _load_profiles(self) -> dict[str, dict[str, Any]]:
        """Parse ``openapi.profile.<name>.<key>`` schema annotations into profile dicts.

        Linkml-runtime rejects nested-dict annotation values and unknown
        top-level keys, so the profile config is encoded as flat dotted
        annotation tags. Comma-separated string values are split into
        lists for the four ``*_classes`` / ``*_slots`` keys; the
        ``description`` key keeps its raw string.
        """
        annotations = getattr(self.schemaview.schema, "annotations", None) or {}
        if not annotations:
            return {}
        profiles: dict[str, dict[str, Any]] = {}
        items = annotations.values() if hasattr(annotations, "values") else annotations
        for ann in items:
            tag = getattr(ann, "tag", None)
            if not tag or not str(tag).startswith("openapi.profile."):
                continue
            rest = str(tag)[len("openapi.profile.") :]
            if "." not in rest:
                continue
            name, key = rest.rsplit(".", 1)
            value = str(getattr(ann, "value", "") or "")
            profile = profiles.setdefault(name, {})
            if key in self._PROFILE_LIST_KEYS:
                profile[key] = _parse_csv(value)
            elif key in self._PROFILE_STR_KEYS:
                profile[key] = value
            else:
                import warnings

                warnings.warn(
                    f"Unknown profile key {key!r} in annotation {tag!r}; "
                    "expected one of "
                    + ", ".join(self._PROFILE_LIST_KEYS + self._PROFILE_STR_KEYS),
                    stacklevel=2,
                )
        return profiles

    def _resolve_profile_filter(self) -> tuple[set[str], set[str], str | None]:
        """Resolve the active profile and validate it.

        Returns ``(excluded_classes, excluded_slots, description)``. Raises
        on an unknown profile name or when an excluded slot is referenced
        by ``openapi.path_variable`` / ``openapi.query_param`` (drift
        detection — the spec would be broken silently otherwise).
        """
        if not self.profile:
            return (set(), set(), None)
        profiles = self._load_profiles()
        if self.profile not in profiles:
            available = sorted(profiles) or ["<none declared>"]
            raise ValueError(
                f"Unknown profile {self.profile!r}. Available profiles: "
                f"{', '.join(available)}. Declare via `openapi.profile."
                f"{self.profile}.<key>` schema annotations."
            )
        p = profiles[self.profile]
        excluded_classes = set(p.get("exclude_classes", []))
        excluded_slots = set(p.get("exclude_slots", []))
        self._raise_on_drift(excluded_classes, excluded_slots)
        return excluded_classes, excluded_slots, p.get("description")

    def _raise_on_drift(self, excluded_classes: set[str], excluded_slots: set[str]) -> None:
        """Fail when an excluded slot is referenced by an annotation.

        ``openapi.path_variable`` and ``openapi.query_param`` would point
        at slots that no longer exist on the emitted schemas — the spec
        would be valid YAML but operationally broken. Surface that as a
        generation-time error.
        """
        sv = self.schemaview
        for class_name in sv.all_classes():
            if class_name in excluded_classes:
                continue
            cls = sv.get_class(class_name)
            for slot in sv.class_induced_slots(class_name):
                if slot.name not in excluded_slots:
                    continue
                pv = self._get_slot_annotation(cls, slot.name, "openapi.path_variable")
                qp = self._get_slot_annotation(cls, slot.name, "openapi.query_param")
                if not (pv or qp):
                    continue
                tags = []
                if pv:
                    tags.append("openapi.path_variable")
                if qp:
                    tags.append("openapi.query_param")
                raise ValueError(
                    f"Profile {self.profile!r} excludes slot {slot.name!r} on "
                    f"{class_name!r}, but the slot is annotated with "
                    f"{' / '.join(tags)}. Remove the annotation, drop "
                    "the slot from exclude_slots, or exclude the whole class."
                )

    def _is_slot_excluded(self, slot: SlotDefinition) -> bool:
        """True when the active profile filters this slot out of generated schemas.

        A slot is excluded when:
          - its name is in ``exclude_slots``, or
          - its range is an excluded class (the target schema won't exist).
        """
        if slot.name in self._excluded_slots:
            return True
        if slot.range and slot.range in self._excluded_classes:
            return True
        return False

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

    # --- Discriminator / polymorphism (issue #20) --------------------------

    def _designates_type_slot(self, class_name: str) -> SlotDefinition | None:
        """Return the slot with `designates_type: true` on this class, or None."""
        for slot in self.schemaview.class_induced_slots(class_name):
            if getattr(slot, "designates_type", False):
                return slot
        return None

    def _discriminator_field(self, cls: ClassDefinition) -> str | None:
        """The discriminator field name for `cls`, or None.

        Reads (in priority order):
          1. ``openapi.discriminator: <field>`` class annotation
          2. ``designates_type: true`` on a slot induced into the class

        Setting both at once is a generation-time error — they say the
        same thing two ways.
        """
        annotation_field = self._class_annotation(cls, "openapi.discriminator")
        designates_slot = self._designates_type_slot(cls.name)
        if annotation_field is not None and designates_slot is not None:
            raise ValueError(
                f"Class {cls.name!r} declares both designates_type "
                f"(on slot {designates_slot.name!r}) and "
                f"openapi.discriminator. Remove one — they describe the same "
                "discriminator two different ways."
            )
        if annotation_field is not None:
            return annotation_field.strip()
        if designates_slot is not None:
            return designates_slot.name
        return None

    def _is_discriminator_root(self, cls: ClassDefinition, field: str) -> bool:
        """True when ``cls`` is the topmost class declaring ``field`` as discriminator.

        ``designates_type`` and ``class_induced_slots`` propagate through the
        ``is_a`` chain, so a concrete leaf naively reports the same
        discriminator as its abstract parent. Only the root should emit the
        ``discriminator`` block.
        """
        if not cls.is_a:
            return True
        parent_cls = self.schemaview.get_class(cls.is_a)
        if parent_cls is None:
            return True
        parent_field = self._discriminator_field(parent_cls)
        return parent_field != field

    def _concrete_descendants_including_self(self, class_name: str) -> list[str]:
        """Concrete (non-abstract, non-mixin) descendants plus self if concrete.

        Mixins are excluded entirely from polymorphic mappings — they're
        trait composition, not subtyping.
        """
        sv = self.schemaview
        out: list[str] = []
        for name in [class_name] + list(sv.class_descendants(class_name, reflexive=False)):
            cls = sv.get_class(name)
            if cls is None or cls.abstract or cls.mixin:
                continue
            out.append(name)
        return out

    def _type_value(self, cls: ClassDefinition) -> str:
        """The discriminator value for a concrete subclass.

        Defaults to the class name as-is, matching ``designates_type``'s
        LinkML default. ``openapi.type_value`` overrides — used when an
        existing system has a fixed value the schema needs to honour.
        """
        override = self._class_annotation(cls, "openapi.type_value")
        return override.strip() if override else cls.name

    def _apply_discriminators(self, schemas: dict[str, Schema | Reference]) -> None:
        """Patch component schemas with OpenAPI ``discriminator`` blocks.

        For each class that declares a discriminator (via ``designates_type``
        or ``openapi.discriminator``), attaches:
          - a ``discriminator`` block on the parent's component schema
          - a ``required``-with-enum form of the discriminator field
            (synthesizing the property if the LinkML side didn't declare it)
          - a single-value enum + default on every concrete descendant's
            local property block
        """
        sv = self.schemaview
        for class_name in sv.all_classes():
            cls = sv.get_class(class_name)
            field = self._discriminator_field(cls)
            if field is None:
                continue
            if not self._is_discriminator_root(cls, field):
                continue
            concrete = self._concrete_descendants_including_self(class_name)
            if not concrete:
                # Discriminator declared but nothing concrete to dispatch
                # to — the parent is itself abstract and has no concrete
                # subclasses. Skip silently; the schema is still valid.
                continue

            mapping: dict[str, str] = {}
            seen: dict[str, str] = {}
            for sub_name in concrete:
                tv = self._type_value(sv.get_class(sub_name))
                if tv in seen:
                    raise ValueError(
                        f"Duplicate openapi.type_value {tv!r} on classes "
                        f"{seen[tv]!r} and {sub_name!r}; values must be unique "
                        "across a discriminator group."
                    )
                seen[tv] = sub_name
                mapping[tv] = f"#/components/schemas/{sub_name}"

            self._inject_discriminator_on_parent(
                schemas, class_name, field, list(mapping.keys()), mapping
            )
            for tv, sub_name in seen.items():
                self._inject_subclass_type_value(schemas, sub_name, field, tv)

    def _inject_discriminator_on_parent(
        self,
        schemas: dict[str, Schema | Reference],
        class_name: str,
        field: str,
        type_values: list[str],
        mapping: dict[str, str],
    ) -> None:
        schema = schemas.get(class_name)
        if not isinstance(schema, Schema):
            return  # Reference — nothing to patch

        # The discriminator goes at the schema-object level so it applies to
        # any $ref that resolves to this schema.
        schema.discriminator = Discriminator(propertyName=field, mapping=mapping)

        # Locate or synthesise the discriminator field. With `is_a`
        # inheritance the local properties live under `allOf[1]`; for
        # standalone classes they're at the top level.
        local = self._writable_local_schema(schema)
        properties = local.properties or {}
        existing = properties.get(field)
        if existing is None or not isinstance(existing, Schema):
            properties[field] = Schema(type=DataType.STRING, enum=type_values)
        else:
            existing.type = DataType.STRING
            existing.enum = type_values
        local.properties = properties

        required = list(local.required or [])
        if field not in required:
            required.append(field)
        local.required = required

    @staticmethod
    def _writable_local_schema(schema: Schema) -> Schema:
        """Return the Schema where local properties live (top-level or allOf[1])."""
        if schema.allOf:
            for part in schema.allOf:
                if isinstance(part, Schema):
                    return part
            # No inline part — append one so we have somewhere to put properties
            inline = Schema(type=DataType.OBJECT)
            schema.allOf.append(inline)
            return inline
        return schema

    def _inject_subclass_type_value(
        self,
        schemas: dict[str, Schema | Reference],
        class_name: str,
        field: str,
        type_value: str,
    ) -> None:
        schema = schemas.get(class_name)
        if not isinstance(schema, Schema):
            return
        local = self._writable_local_schema(schema)
        properties = dict(local.properties or {})
        # Single-value enum so a hand-written client reading the spec sees
        # exactly what to send for this concrete subclass.
        properties[field] = Schema(
            type=DataType.STRING,
            enum=[type_value],
            default=type_value,
        )
        local.properties = properties
        required = list(local.required or [])
        if field not in required:
            required.append(field)
        local.required = required

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

    # --- PATCH (issue #16) -------------------------------------------------

    def _make_patch_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        """Emit a PATCH item operation using JSON Merge Patch (RFC 7396).

        Request body media type is fixed at ``application/merge-patch+json``
        — RFC 7396 is JSON-specific, so the class's ``openapi.media_types``
        do not apply to the request side. The 200 response uses the full
        class schema and honours ``openapi.media_types`` as usual.
        """
        media_types = self._get_media_types(cls)
        full_ref = Reference(ref=f"#/components/schemas/{class_name}")
        patch_ref = Reference(ref=f"#/components/schemas/{class_name}Patch")
        return Operation(
            summary=f"Patch a {class_name}",
            operationId=f"patch_{_to_snake_case(class_name)}",
            tags=[class_name],
            requestBody=RequestBody(
                required=True,
                content={
                    "application/merge-patch+json": MediaType(media_type_schema=patch_ref),
                },
                description=(
                    "Partial update per RFC 7396. Omit fields you don't want to change. "
                    "Sending null clears the field; sending a value sets it."
                ),
            ),
            responses={
                "200": Response(
                    description=f"{class_name} patched",
                    content=self._content_for(full_ref, media_types),
                ),
                "404": self._error_response("Not found"),
                "422": self._error_response("Validation error"),
            },
        )

    def _build_patch_schema(self, class_name: str, cls: ClassDefinition) -> Schema:
        """Build a ``<Class>Patch`` schema: every induced slot, all optional.

        Identifier slots are excluded — the path already names the
        resource and merge-patch can't cleanly express "leave id alone"
        on a non-nullable string.

        ``x-rdf-class`` and ``x-rdf-property`` mappings are recorded so
        downstream RDF tooling that reads the spec can apply patches as
        named-graph mutations.
        """
        sv = self.schemaview
        patch_name = f"{class_name}Patch"
        if cls.class_uri:
            self._x_rdf_class[patch_name] = sv.expand_curie(cls.class_uri)

        properties: dict[str, Schema | Reference] = {}
        for slot in sv.class_induced_slots(class_name):
            if slot.identifier:
                continue
            properties[slot.name] = self._slot_to_schema(slot)
            if slot.slot_uri:
                self._x_rdf_property[(patch_name, slot.name)] = sv.expand_curie(slot.slot_uri)

        schema = Schema(type=DataType.OBJECT, additionalProperties=False)
        schema.title = patch_name
        schema.description = (
            f"Partial update for {class_name}. All fields optional; semantics "
            "per RFC 7396 (JSON Merge Patch). The identifier is excluded — "
            "the path already names the resource."
        )
        if properties:
            schema.properties = properties
        return schema

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

    def _class_path_id_name(self, class_name: str) -> str:
        """Path-variable name to use whenever ``class_name`` appears in a URL.

        Drives both the leaf ``{<class>_id}`` segment in a single-level nested
        item path and every ancestor segment in an N-level chain. Default is
        ``<snake>_id`` so existing specs stay byte-identical; override per
        class with the ``openapi.path_id`` annotation::

            classes:
              Catalog:
                annotations:
                  openapi.path_id: catalogId   # → {catalogId} in URLs
        """
        cls = self.schemaview.get_class(class_name)
        override = self._class_annotation(cls, "openapi.path_id") if cls else None
        if override:
            return override.strip()
        return f"{_to_snake_case(class_name)}_id"

    def _nested_item_path_var(self, target_class_name: str) -> str:
        """Path-variable name for the linked item under a nested path.

        Avoids colliding with the parent's ``{id}`` by namespacing on the
        target class (e.g. ``/books/{id}/authors/{author_id}``). Override
        per class with ``openapi.path_id``.
        """
        return self._class_path_id_name(target_class_name)

    def _make_nested_paths(
        self,
        parent_class_name: str,
        parent_path_segment: str,
        parent_path_vars: list,
    ) -> dict[str, PathItem]:
        """Single-level nested paths under a class's own item path.

        Thin wrapper that renders ``parent_path_segment`` + ``parent_path_vars``
        into a URL prefix and the matching parameter list, then delegates to
        :meth:`_make_nested_paths_with_prefix` so single-level and N-level
        emission share the same code path. Honours the parent's
        ``openapi.path_id`` so the nested URL and the class's own flat item
        path use the same parameter name.
        """
        parent_cls = self.schemaview.get_class(parent_class_name)
        path_id_override = (
            self._class_annotation(parent_cls, "openapi.path_id")
            if parent_cls and len(parent_path_vars) == 1
            else None
        )
        named_vars = [
            (
                path_id_override.strip() if path_id_override else slot.name,
                slot,
                mode,
            )
            for slot, mode in parent_path_vars
        ]
        parent_var_suffix = "/".join(f"{{{name}}}" for name, _slot, _mode in named_vars)
        prefix = (
            f"{parent_path_segment}/{parent_var_suffix}"
            if parent_var_suffix
            else parent_path_segment
        )
        parent_path_params = [
            Parameter(
                name=name,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._path_variable_schema(slot, mode),
            )
            for name, slot, mode in named_vars
        ]
        return self._make_nested_paths_with_prefix(parent_class_name, prefix, parent_path_params)

    def _make_nested_paths_with_prefix(
        self,
        parent_class_name: str,
        url_prefix: str,
        path_params: list[Parameter],
    ) -> dict[str, PathItem]:
        """Generate nested paths for composition and reference relationships.

        Walks every multivalued, class-ranged slot on the parent and emits:

        - **Composition** (``slot.inlined``): full CRUD nested at
          ``/{prefix}/{slot}`` and ``/{prefix}/{slot}/{target_id}`` when the
          target has an identifier.
        - **Reference** (target has identifier, ``inlined: false``): GET to
          list, POST with a ``ResourceLink`` body to attach (single or batch
          via ``oneOf``), DELETE on the per-target item path to detach.

        ``url_prefix`` is the already-rendered ancestor portion of the URL
        without leading or trailing slashes, e.g. ``"catalogs/{id}"`` for a
        single-level emission or
        ``"orgs/{org_id}/catalogs/{catalog_id}/datasets/{dataset_id}"`` for an
        N-level emission. ``path_params`` carries every `Parameter` that
        prefix references, so the resulting `PathItem` lists them at the
        path level.
        """
        sv = self.schemaview
        parent_cls = sv.get_class(parent_class_name)
        out: dict[str, PathItem] = {}

        for slot in sv.class_induced_slots(parent_class_name):
            if not slot.multivalued:
                continue
            if self._is_slot_excluded(slot):
                continue
            target_name = slot.range
            if not target_name or sv.get_class(target_name) is None:
                continue  # primitive, enum, or unresolved range

            # Opt-out: a slot may carry `openapi.nested: "false"` to suppress
            # nested-path generation (e.g. back-references that aren't a
            # browseable collection, or relationships exposed elsewhere).
            nested_ann = self._get_slot_annotation(parent_cls, slot.name, "openapi.nested")
            if nested_ann is not None and not _is_truthy(nested_ann):
                continue

            collection_path = f"/{url_prefix}/{slot.name}"
            target_id_slot = self._identifier_slot(target_name)

            if self._is_composition(slot):
                self._add_composition_paths(
                    out,
                    collection_path,
                    parent_class_name,
                    slot,
                    target_name,
                    target_id_slot,
                    path_params,
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
                    path_params,
                )

        # Synthetic inverse paths — emitted for `inverse:` declarations
        # whose target slot doesn't actually exist on this class. Always
        # reference-shaped (composition can't be inverted: a composed
        # child has no independent identity to put on the wire).
        for synth_name, source_class in self._synthetic_inverses_for(parent_class_name):
            collection_path = f"/{url_prefix}/{synth_name}"
            target_id_slot = self._identifier_slot(source_class)
            fake_slot = SlotDefinition(name=synth_name, range=source_class, multivalued=True)
            self._needs_resource_link = True
            self._add_reference_paths(
                out,
                collection_path,
                parent_class_name,
                fake_slot,
                source_class,
                target_id_slot,
                path_params,
            )

        return out

    def _synthetic_inverses_for(self, target_class_name: str) -> list[tuple[str, str]]:
        """Inverse declarations that name a slot not present on ``target_class_name``.

        Reads from the ``self._synthetic_inverses_index`` precomputed once
        per build by ``_collect_synthetic_inverses`` — without that cache,
        the per-class lookup would be O(all_classes × max_slots) and the
        per-build cost O(resource_classes × all_classes × max_slots).
        """
        return self._synthetic_inverses_index.get(target_class_name, [])

    def _collect_synthetic_inverses(self) -> dict[str, list[tuple[str, str]]]:
        """Build the per-target index of inverse-direction slots to synthesise.

        For each ``inverse: TargetClass.slot_name`` declaration whose named
        slot doesn't already exist on the target, record one entry under
        ``TargetClass``. Excluded source classes and excluded slot names
        are skipped so the index reflects the active profile.
        """
        sv = self.schemaview
        index: dict[str, list[tuple[str, str]]] = {}
        # Cache target_class_name → set of its existing slot names so we
        # don't rebuild it once per declaration.
        target_slot_names_cache: dict[str, set[str]] = {}
        seen_per_target: dict[str, set[str]] = {}
        for src_class_name in sv.all_classes():
            if src_class_name in self._excluded_classes:
                continue
            for slot in sv.class_induced_slots(src_class_name):
                if not slot.inverse or "." not in str(slot.inverse):
                    continue
                tgt_class, tgt_slot_name = str(slot.inverse).split(".", 1)
                if tgt_slot_name in self._excluded_slots:
                    continue
                if tgt_class not in target_slot_names_cache:
                    target_slot_names_cache[tgt_class] = {
                        s.name for s in sv.class_induced_slots(tgt_class)
                    }
                if tgt_slot_name in target_slot_names_cache[tgt_class]:
                    continue  # the target has a real slot — emits naturally
                seen = seen_per_target.setdefault(tgt_class, set())
                if tgt_slot_name in seen:
                    continue  # another source already synthesised this slot
                seen.add(tgt_slot_name)
                index.setdefault(tgt_class, []).append((tgt_slot_name, src_class_name))
        return index

    # --- Deep nested paths via parent-chain walk -------------------------

    def _collect_parent_chains(self) -> dict[str, list[list[tuple[str, str]]]]:
        """Index every chain of `(parent_class, slot_name)` leading to each class.

        For a leaf class ``L``, each chain is the ordered list of ancestors
        from the root parent down to the direct parent — e.g. for
        ``Org → Catalog → Dataset → Distribution`` walked via
        ``Org.catalogs``, ``Catalog.datasets``, ``Dataset.distributions``,
        the chain is::

            [("Org", "catalogs"),
             ("Catalog", "datasets"),
             ("Dataset", "distributions")]

        Chains are pruned at slots annotated ``openapi.nested: "false"``
        and skip excluded classes / slots so the index reflects the active
        profile. Cycles are detected (the recursion tracks visited classes
        on the current path) so a graph with ``A.bs: list[B]`` and
        ``B.as: list[A]`` doesn't blow up. Only ancestors that are
        themselves resource classes contribute — non-resource intermediates
        have no addressable URL segment to fold in.

        Result is a dict keyed by leaf class name. A class with no parents
        in the relationship graph is absent from the dict.
        """
        sv = self.schemaview
        resource_classes = set(self._get_resource_classes())

        direct_parents: dict[str, list[tuple[str, str]]] = {}
        for parent_name in sv.all_classes():
            if parent_name in self._excluded_classes:
                continue
            if parent_name not in resource_classes:
                continue
            parent_cls = sv.get_class(parent_name)
            for slot in sv.class_induced_slots(parent_name):
                if not slot.multivalued:
                    continue
                if self._is_slot_excluded(slot):
                    continue
                target = slot.range
                if not target or sv.get_class(target) is None:
                    continue
                if target == parent_name:
                    continue  # self-loop, no canonical chain
                nested_ann = self._get_slot_annotation(parent_cls, slot.name, "openapi.nested")
                if nested_ann is not None and not _is_truthy(nested_ann):
                    continue
                direct_parents.setdefault(target, []).append((parent_name, slot.name))

        index: dict[str, list[list[tuple[str, str]]]] = {}

        def walk(leaf: str, on_path: tuple[str, ...]) -> list[list[tuple[str, str]]]:
            if leaf in index:
                # Memoised — but we still need to filter chains that would
                # introduce a cycle from the caller's perspective.
                return [c for c in index[leaf] if not any(p in on_path for p, _ in c)]
            chains: list[list[tuple[str, str]]] = []
            for parent_name, slot_name in direct_parents.get(leaf, []):
                if parent_name in on_path:
                    continue  # would close a cycle
                upper = walk(parent_name, on_path + (parent_name,))
                if not upper:
                    chains.append([(parent_name, slot_name)])
                else:
                    for u in upper:
                        chains.append(u + [(parent_name, slot_name)])
            index[leaf] = chains
            return chains

        for cls_name in list(direct_parents.keys()):
            walk(cls_name, ())
        # Drop classes with no chains so callers can skip via membership check.
        return {k: v for k, v in index.items() if v}

    @staticmethod
    def _suffix_operation_ids(paths: dict[str, PathItem], suffix: str) -> None:
        """Append ``suffix`` to every ``operationId`` in a paths dict.

        Deep nested paths reuse the flat operation builders, which means
        their ``operationId`` collides with the same class's flat-item
        operations and with its own single-level nested walk. OpenAPI
        requires globally unique operation IDs, so we patch the IDs in
        place after the deep paths are built.
        """
        methods = ("get", "put", "post", "delete", "patch", "options", "head", "trace")
        for path_item in paths.values():
            for method in methods:
                op = getattr(path_item, method, None)
                if op is None:
                    continue
                existing = getattr(op, "operationId", None)
                if existing:
                    op.operationId = existing + suffix

    @staticmethod
    def _parent_path_segments(annotation: str) -> list[tuple[str | None, str]]:
        """Parse an ``openapi.parent_path`` annotation into per-hop matchers.

        Each dot-separated segment is either ``slot_name`` (match the slot
        at that hop, parent class implied) or ``ClassName.slot_name``
        (match both the parent class and the slot — used to disambiguate
        when multiple parents share a slot name).

        Returns a list of ``(class_name_or_none, slot_name)`` tuples,
        one per hop.
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

    def _canonical_parent_chain(self, class_name: str) -> list[tuple[str, str]]:
        """Pick the canonical chain for ``class_name``.

        * 0 chains → returns ``[]`` (the class is a root, no deep paths).
        * 1 chain → returns it.
        * >1 chains → reads ``openapi.parent_path`` on the leaf and matches.

        Annotation syntax: ``/``-separated hops, each hop ``slot_name`` or
        ``ClassName.slot_name``. Examples::

            openapi.parent_path: catalogs.datasets        # two hops, slot-only (unambiguous)
            openapi.parent_path: Folder.tags              # one hop, class-qualified
            openapi.parent_path: Org.catalogs/Catalog.datasets  # two hops, fully qualified

        Raises with the candidate list when the annotation is missing on
        an ambiguous leaf or doesn't match any chain.
        """
        chains = self._parent_chains_index.get(class_name, [])
        if not chains:
            return []
        if len(chains) == 1:
            return chains[0]

        cls = self.schemaview.get_class(class_name)
        annotated = self._class_annotation(cls, "openapi.parent_path") if cls else None
        # Build human-readable candidate strings: prefer slot-only when it's
        # unique at every hop, fall back to class-qualified otherwise.
        candidates_qualified = ["/".join(f"{p}.{s}" for p, s in chain) for chain in chains]
        if annotated:
            wanted = self._parent_path_segments(annotated)
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
                f"`openapi.parent_path: {annotated!r}` but no matching chain "
                f"exists. Candidates: {candidates_qualified}."
            )
        raise ValueError(
            f"Class {class_name!r} is reachable via multiple parent chains. "
            f"Pick one with the `openapi.parent_path` class annotation, "
            f"e.g. `openapi.parent_path: {candidates_qualified[0]!r}`. "
            f"Candidates: {candidates_qualified}."
        )

    def _build_chain_path_params(self, chain: list[tuple[str, str]]) -> tuple[str, list[Parameter]]:
        """Render a chain as a URL prefix and the matching path-parameter list.

        Given ``[(Org, "catalogs"), (Catalog, "datasets")]`` produces::

            ("orgs/{orgId}/catalogs/{catalogId}/datasets",
             [<Parameter orgId>, <Parameter catalogId>])

        Only the first hop carries the root class's path segment; subsequent
        hops skip it because the previous iteration's slot name already
        identifies the next collection (the slot ``Org.catalogs`` *is* the
        URL noun for catalogs reached through that org). Each parent's
        identifier variable uses ``_class_path_id_name`` so ``openapi.path_id``
        overrides flow through.
        """
        if not chain:
            return "", []
        sv = self.schemaview
        prefix_parts: list[str] = []
        params: list[Parameter] = []
        for i, (parent_name, slot_name) in enumerate(chain):
            id_slot = self._identifier_slot(parent_name)
            if id_slot is None:
                # Should not happen for a class that's a resource, but guard
                # so a misconfigured schema fails loudly rather than silently.
                raise ValueError(
                    f"Parent class {parent_name!r} in a deep nested chain "
                    "has no identifier slot — can't synthesise its path "
                    "parameter."
                )
            param_name = self._class_path_id_name(parent_name)
            if i == 0:
                parent_cls = sv.get_class(parent_name)
                parent_segment = self._get_path_segment(parent_cls)
                prefix_parts.append(f"{parent_segment}/{{{param_name}}}/{slot_name}")
            else:
                prefix_parts.append(f"{{{param_name}}}/{slot_name}")
            params.append(
                Parameter(
                    name=param_name,
                    param_in=ParameterLocation.PATH,
                    required=True,
                    param_schema=self._slot_to_schema(id_slot),
                )
            )
        return "/".join(prefix_parts), params

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

    # Slot ranges over which `comparable` operators (>=, <=, >, <) are
    # well-defined. String ranges are technically lex-comparable but the
    # intent is almost always "numeric / temporal range" — warn if asked.
    _COMPARABLE_RANGES = frozenset({"integer", "float", "double", "decimal", "date", "datetime"})

    _QUERY_PARAM_TOKENS = frozenset({"equality", "comparable", "sortable"})

    def _query_param_capabilities(self, cls: ClassDefinition, slot_name: str) -> set[str] | None:
        """Parse the slot's `openapi.query_param` annotation into a capability set.

        Accepted tokens (comma-separated):

          ``"true"`` / ``"equality"``  — exact-match query param
          ``"comparable"``             — equality + ``__gte`` / ``__lte`` / ``__gt`` / ``__lt``
          ``"sortable"``               — equality + token in ``?sort=`` array

        ``comparable`` and ``sortable`` imply ``equality`` (most APIs that filter
        by range also filter by exact match). Returns ``None`` when the
        annotation is absent or explicitly false. Unknown tokens are warned
        about so typos like ``"sorteable"`` don't silently disable filtering.
        """
        raw = self._get_slot_annotation(cls, slot_name, "openapi.query_param")
        if raw is None:
            return None
        tokens = set(_parse_csv(raw, lowercase=True))
        if not tokens or tokens == {"false"}:
            return None
        unknown = tokens - {"true", "false"} - self._QUERY_PARAM_TOKENS
        if unknown:
            import warnings

            warnings.warn(
                f"Slot {cls.name}.{slot_name!r} declares unknown "
                f"openapi.query_param token(s) {sorted(unknown)!r}; "
                f"expected one or more of {sorted(self._QUERY_PARAM_TOKENS)!r}. "
                "Token(s) ignored — fix the typo or remove them.",
                stacklevel=3,
            )
        if "true" in tokens:
            tokens.add("equality")
            tokens.discard("true")
        if "comparable" in tokens or "sortable" in tokens:
            tokens.add("equality")
        valid = tokens & self._QUERY_PARAM_TOKENS
        return valid or None

    def _auto_query_params_enabled(self, cls: ClassDefinition) -> bool:
        """Whether auto-inferred query parameters are emitted for this class.

        Class-level ``openapi.auto_query_params`` wins over the schema-level
        annotation; default is ``True`` so existing schemas keep their
        current behaviour. Set to ``"false"`` at either level to suppress
        the auto-inferred filter parameters on a class with many slots.
        """
        class_value = self._class_annotation(cls, "openapi.auto_query_params")
        if class_value is not None:
            return _is_truthy(class_value)
        schema_value = self._schema_annotation("openapi.auto_query_params")
        if schema_value is not None:
            return _is_truthy(schema_value)
        return True

    def _make_query_params(self, cls: ClassDefinition) -> list[Parameter]:
        """Generate query parameters for the list endpoint.

        Annotated slots win when any are present on the class; otherwise
        the auto-inference path picks scalar non-identifier slots — unless
        ``openapi.auto_query_params: "false"`` is set at the class or
        schema level, in which case only ``limit`` / ``offset`` emit.
        Slots annotated ``openapi.query_param: "false"`` are excluded from
        auto-inference even when it is enabled. Both paths walk induced
        slots once.
        """
        sv = self.schemaview
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

        auto_enabled = self._auto_query_params_enabled(cls)

        annotated_params: list[Parameter] = []
        inferred_params: list[Parameter] = []
        sort_tokens: list[str] = []

        for slot in sv.class_induced_slots(cls.name):
            if self._is_slot_excluded(slot):
                continue
            caps = self._query_param_capabilities(cls, slot.name)
            if caps is not None:
                self._add_annotated_query_params(cls, slot, caps, annotated_params, sort_tokens)
                continue
            if not auto_enabled:
                continue
            # Slot-level explicit opt-out: `openapi.query_param: "false"`
            # makes ``_query_param_capabilities`` return ``None``, but the
            # raw annotation tells us the user explicitly removed this slot
            # from auto-inference (vs. being silent about it).
            raw = self._get_slot_annotation(cls, slot.name, "openapi.query_param")
            if raw is not None and set(_parse_csv(raw, lowercase=True)) == {"false"}:
                continue
            if (
                not slot.multivalued
                and not slot.identifier
                and (
                    (slot.range or "string") in ("string", "integer", "boolean")
                    or sv.get_enum(slot.range or "string")
                )
            ):
                inferred_params.append(
                    Parameter(
                        name=slot.name,
                        param_in=ParameterLocation.QUERY,
                        required=False,
                        param_schema=self._slot_to_schema(slot),
                    )
                )

        if annotated_params or sort_tokens:
            params.extend(annotated_params)
            if sort_tokens:
                params.append(self._make_sort_param(sort_tokens))
            return params

        params.extend(inferred_params)
        return params

    def _add_annotated_query_params(
        self,
        cls: ClassDefinition,
        slot: SlotDefinition,
        caps: set[str],
        out: list[Parameter],
        sort_tokens: list[str],
    ) -> None:
        """Emit equality / comparison / sort entries for one annotated slot."""
        range_name = slot.range or "string"
        if "equality" in caps:
            out.append(
                Parameter(
                    name=slot.name,
                    param_in=ParameterLocation.QUERY,
                    required=False,
                    param_schema=self._slot_to_schema(slot),
                )
            )
        if "comparable" in caps:
            if range_name not in self._COMPARABLE_RANGES:
                import warnings

                warnings.warn(
                    f"Slot {cls.name}.{slot.name!r} marked `comparable` but "
                    f"range {range_name!r} is not a numeric or temporal type; "
                    "comparison operators may behave unexpectedly.",
                    stacklevel=3,
                )
            for op in ("gte", "lte", "gt", "lt"):
                out.append(
                    Parameter(
                        name=f"{slot.name}__{op}",
                        param_in=ParameterLocation.QUERY,
                        required=False,
                        param_schema=self._slot_to_schema(slot),
                    )
                )
        if "sortable" in caps:
            if slot.multivalued:
                raise ValueError(
                    f"Slot {cls.name}.{slot.name!r} is multivalued; sort "
                    "order over a set is not well-defined. Remove `sortable` "
                    "or change the slot to single-valued."
                )
            sort_tokens.extend([slot.name, f"-{slot.name}"])

    @staticmethod
    def _make_sort_param(sort_tokens: list[str]) -> Parameter:
        return Parameter(
            name="sort",
            param_in=ParameterLocation.QUERY,
            required=False,
            description=(
                "Comma-separated list of slot names to sort by. "
                "Prefix a name with `-` for descending."
            ),
            style="form",
            explode=False,
            param_schema=Schema(
                type=DataType.ARRAY,
                items=Schema(type=DataType.STRING, enum=sort_tokens),
            ),
        )
