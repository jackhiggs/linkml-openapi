"""OpenAPI 3.1 generator for LinkML schemas.

Converts LinkML schema definitions into OpenAPI 3.1 specifications with:
- JSON Schema components derived from classes and slots
- CRUD endpoints for classes annotated with openapi.resource: true
- Path/query parameter inference from slot annotations
"""

import json
import os
import re
import warnings
from dataclasses import dataclass, field
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
from linkml_openapi._chains import (
    PATH_TEMPLATE_PLACEHOLDER_RE,
    build_parent_chains_index,
    canonical_parent_chain,
)
from linkml_openapi._chains import (
    parse_path_param_sources as _parse_path_param_sources_helper,
)
from linkml_openapi._query_params import (
    QueryParamSpec,
    walk_query_params,
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


# URL path-segment styles. Module-level so the CLI can import the
# canonical list of choices without re-listing it.
PATH_STYLE_SNAKE = "snake_case"
PATH_STYLE_KEBAB = "kebab-case"
SUPPORTED_PATH_STYLES: frozenset[str] = frozenset({PATH_STYLE_SNAKE, PATH_STYLE_KEBAB})

# Operation tokens accepted by `openapi.operations`. Order matters for
# the default emission tuple — list/create on collection, then item ops.
OP_LIST = "list"
OP_CREATE = "create"
OP_READ = "read"
OP_UPDATE = "update"
OP_PATCH = "patch"
OP_DELETE = "delete"
DEFAULT_OPERATIONS: tuple[str, ...] = (OP_LIST, OP_CREATE, OP_READ, OP_UPDATE, OP_DELETE)
# Operations that need an item path (i.e. an addressable identifier).
ITEM_OPERATIONS: frozenset[str] = frozenset({OP_READ, OP_UPDATE, OP_PATCH, OP_DELETE})


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
    # URL path-segment convention. None falls back to the schema-level
    # `openapi.path_style` annotation (default `"snake_case"`). Set to
    # `"kebab-case"` here or in the schema to render slot- and
    # class-derived path segments with `-` instead of `_`. Slot
    # identifiers in the OpenAPI body, operation IDs, tags, and RDF
    # extensions are unaffected — only the URL segment changes.
    path_style: str | None = None
    # URL path prefix prepended to every emitted ``paths:`` key. None
    # falls back to the schema-level ``openapi.path_prefix`` annotation,
    # then no prefix. Single transformation point — applied at the end
    # of ``_build_openapi`` so every emitter (composition, chain,
    # templated, synthetic-inverse) picks it up uniformly. ``servers[0].url``
    # is intentionally left alone; if a deployment wants the prefix in
    # the server URL too, pass ``--server-url`` explicitly.
    path_prefix: str | None = None
    # Names of registered post-processors to apply (in order) after the
    # canonical spec is built but before serialisation. See
    # ``linkml_openapi.post_processors`` for the registry. Each
    # post-processor adapts the spec for a specific consumer
    # (codegens, validators, JSON-LD adapters) without changing the
    # generator's authoring-clarity defaults.
    post_processors: list[str] = field(default_factory=list)

    def serialize(self, **kwargs) -> str:
        """Generate and serialize the OpenAPI spec."""
        # Reset the x-rdf-* maps; _build_openapi populates them as it walks the schema.
        self._x_rdf_class: dict[str, str] = {}
        self._x_rdf_property: dict[tuple[str, str], str] = {}
        # Per-build cache so the schema-walk helpers (descendants,
        # induced slots, etc.) don't re-traverse the class graph for
        # every reference site. Reset here to pick up any schema-view
        # changes between successive ``serialize()`` calls.
        self._concrete_descendants_cache: dict[str, list[str]] = {}
        # Codegen field-name overrides — accumulated for the companion
        # ``name-mappings`` file. openapi-generator does not have a
        # universal *spec-level* extension that reliably renames a
        # JSON property to a clean target-language identifier
        # (``x-codegen-name`` is operation-only on most templates). The
        # working mechanism is the ``--name-mappings`` CLI flag (or
        # ``@file`` form). We collect every ``wire-name=codegen-name``
        # pair the schema declares and let
        # :meth:`emit_name_mappings` write a sibling file the user
        # passes to ``openapi-generator-cli``.
        self._codegen_name_mappings: dict[str, str] = {}
        # Multi-level composition recursion happens inside
        # `_add_composition_paths`; the stack tracks which composition
        # targets are currently being emitted so a cyclic
        # composition-of-composition chain (`A.bs[B].as[A]`) terminates
        # cleanly instead of recursing forever.
        self._composition_emission_stack: set[str] = set()
        # Resolved name of the schema referenced from non-2xx responses, or
        # None when error_schema is off. Cached per-build so each operation
        # builder doesn't re-resolve.
        self._error_class_name: str | None = self._resolve_error_class()
        # Resolve the active path-style: CLI / Python kwarg wins over the
        # schema-level annotation, which falls back to `"snake_case"`. We
        # validate once here so per-call-site renderers can just check the
        # cached value without re-parsing.
        self._effective_path_style = self._resolve_path_style()
        # Resolve the active path prefix (CLI / Python kwarg → schema
        # annotation → none). Stored as the canonical normalised form
        # (leading "/", no trailing "/") or empty string for "no prefix".
        self._effective_path_prefix = self._resolve_path_prefix()
        # Resolve the active profile (or no-op when self.profile is None).
        # `_resolve_profile_filter` also runs drift detection — failing
        # loudly if an excluded slot is referenced by another annotation.
        (
            self._excluded_classes,
            self._excluded_slots,
            self._profile_description,
        ) = self._resolve_profile_filter()
        # Per-build cache of induced slots keyed `(class_name, slot_name)`.
        # `_get_slot_annotation`, `_render_slot_segment`, and the
        # nested-path / chain emitters used to call
        # `class_induced_slots(name)` and linearly scan the result on
        # every lookup, giving O(slots²) per class. The cache collapses
        # the inner loop to O(1).
        self._induced_slot_cache: dict[str, dict[str, SlotDefinition]] = {}
        # Pre-compute the resource-class list once. `_collect_parent_chains`
        # and `_build_openapi` both need it, and the underlying walk is
        # O(classes × slots).
        self._resource_classes_cache: list[str] | None = None
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
        if self.post_processors:
            from linkml_openapi.post_processors import apply as _apply_post

            raw = _apply_post(raw, list(self.post_processors))
        if self.format == "json":
            return json.dumps(raw, indent=2) + "\n"
        return yaml.dump(raw, default_flow_style=False, sort_keys=False)

    def name_mappings(self) -> dict[str, str]:
        """Return the wire→codegen rename map collected during the build.

        Populated from ``openapi.legacy_type_codegen_name`` (and any
        future codegen-rename annotations). Empty when the schema
        declares no overrides — the file emission step then writes
        nothing rather than an empty stub.

        Must be called after :meth:`serialize` since the build is
        what populates the underlying state.
        """
        return dict(self._codegen_name_mappings)

    def emit_name_mappings(self) -> str:
        """Render the rename map as ``--name-mappings`` file content.

        Format is openapi-generator's expected layout — one
        ``wire-name=codegen-name`` pair per line. Pass to
        ``openapi-generator-cli`` via::

            openapi-generator generate -i spec.yaml -g spring \\
              --name-mappings @name-mappings.txt

        Returns an empty string when no overrides were declared, so
        callers can write the file unconditionally without polluting
        the repo with empty hint files.
        """
        if not self._codegen_name_mappings:
            return ""
        lines = [
            f"{wire}={codegen}" for wire, codegen in sorted(self._codegen_name_mappings.items())
        ]
        return "\n".join(lines) + "\n"

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
        self._validate_inlined_recursion()
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
        # was declared. The component name is whatever
        # `openapi.error_class_name` resolved to (default "Problem"). We
        # only synthesise when no LinkML-defined class supplied the schema —
        # that case is detected by checking whether the resolved name is a
        # class in the schema (a user-defined class already produced its
        # own component) versus an auto-name we own.
        if self._error_class_name and self._error_class_name not in schemas:
            user_defined = self.schemaview.get_class(self._error_class_name) is not None
            if not user_defined:
                schemas[self._error_class_name] = self._build_problem_schema(self._error_class_name)

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
            flat_only = _is_truthy(self._class_annotation(cls, "openapi.flat_only") or False)
            if nested_only and flat_only:
                raise ValueError(
                    f"Class {class_name!r} declares both "
                    '`openapi.nested_only: "true"` and '
                    '`openapi.flat_only: "true"`. They are mutually '
                    "exclusive — pick one. `nested_only` keeps the deep "
                    "URL only; `flat_only` keeps the flat URL only."
                )

            self._validate_resource_addressability(class_name, path_vars, operations)

            # Collection path. Suppressed when `openapi.nested_only: "true"`
            # makes the deep-nested URL the only canonical surface.
            if not nested_only:
                collection_path = f"/{path_segment}"
                collection_item = PathItem()
                if OP_LIST in operations:
                    collection_item.get = self._make_list_operation(cls, class_name)
                if OP_CREATE in operations:
                    collection_item.post = self._make_create_operation(cls, class_name)
                paths[collection_path] = collection_item

            # Item path. When the class declares `openapi.path_id` *and*
            # has exactly one path variable, the override renames the URL
            # parameter (and the matching `Parameter.name`) so the same
            # identifier shows up consistently in the flat item path, in
            # nested-paths-from-this-class, and in any deep chain that
            # passes through this class as an ancestor.
            if path_vars:
                item_suffix, path_params = self._resolve_item_path_vars(class_name, path_vars)

                if not nested_only:
                    item_path = f"/{path_segment}/{item_suffix}"
                    item = PathItem(parameters=path_params)
                    self._attach_item_operations(item, cls, class_name, operations)
                    paths[item_path] = item

                # The PATCH body schema is needed whenever `patch` is
                # listed, regardless of which URL forms emit, so generate
                # it outside the nested-only branch.
                if OP_PATCH in operations and f"{class_name}Patch" not in schemas:
                    schemas[f"{class_name}Patch"] = self._build_patch_schema(class_name, cls)

                paths.update(self._make_nested_paths(class_name, path_segment, path_vars))

                # Deep nested paths. Three strategies, in priority order:
                #
                #   1. `openapi.flat_only: "true"` → skip deep emission
                #      entirely. Flat collection / flat item already emitted
                #      above (when `nested_only` is also off, which the
                #      mutex check guarantees).
                #
                #   2. `openapi.path_template` → user-provided URL template
                #      (Layer 4 escape hatch). Replaces the auto-derived
                #      chain. Parameters are built from
                #      `openapi.path_param_sources` so each `{name}` in the
                #      template binds to a typed `Class.slot` source.
                #
                #   3. Auto-derived chain via the relationship graph
                #      (Layers 1–3). Each ancestor contributes a path
                #      parameter sourced from its identifier slot — *not*
                #      from any field on this class — so the leaf
                #      component schema stays unchanged.
                #
                # Deeper children of this class under a deep prefix are
                # handled naturally as those children's own canonical
                # chain, so each chain depth gets emitted exactly once.
                if not flat_only:
                    template = self._class_annotation(cls, "openapi.path_template")
                    if template:
                        deep_paths = self._emit_templated_deep_path(
                            cls, class_name, template, operations
                        )
                        paths.update(deep_paths)
                    else:
                        chain = self._canonical_parent_chain(class_name)
                        if chain:
                            deep_paths = self._emit_chained_deep_path(
                                cls,
                                class_name,
                                chain,
                                item_suffix,
                                path_params,
                                operations,
                            )
                            paths.update(deep_paths)

        if self._needs_resource_link and "ResourceLink" not in schemas:
            schemas["ResourceLink"] = self._build_resource_link_schema()

        # Apply the path prefix to every emitted `paths:` key. Single
        # transformation point so composition / chain / templated /
        # synthetic-inverse paths all pick it up uniformly. Validates
        # that no class-level ``openapi.path`` already includes the
        # prefix (would produce a doubled prefix like
        # ``/api/v1/api/v1/catalogs``).
        if self._effective_path_prefix:
            paths = self._apply_path_prefix(paths)

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

    def _class_description(self, cls: ClassDefinition) -> str | None:
        """Compose the OpenAPI ``description`` for a class.

        Combines the LinkML ``description`` with an "RDF class:
        ``<curie>``" footer when the class declares ``class_uri``.
        Putting the RDF identity in the description (not just in the
        ``x-rdf-class`` extension) makes it visible directly in
        Swagger UI's schema panel — extensions are only shown by
        scrolling to the small footer area, while ``description`` is
        the prominent text under the schema title.
        """
        parts: list[str] = []
        if cls.description:
            parts.append(cls.description.strip())
        if cls.class_uri:
            parts.append(f"RDF class: `{cls.class_uri}`")
        return "\n\n".join(parts) or None

    def _slot_description(self, slot: SlotDefinition) -> str | None:
        """Compose the OpenAPI ``description`` for a slot, with the
        RDF predicate footer when ``slot_uri`` is set."""
        parts: list[str] = []
        if slot.description:
            parts.append(slot.description.strip())
        if slot.slot_uri:
            parts.append(f"RDF property: `{slot.slot_uri}`")
        return "\n\n".join(parts) or None

    def _class_to_schema(self, cls: ClassDefinition) -> Schema:
        """Convert a LinkML class to a JSON Schema object.

        Inheritance is emitted via ``allOf: [{$ref: parent},
        {properties}]`` so codegens (notably openapi-generator's
        Spring template) produce idiomatic
        ``Catalog extends Dataset extends Resource`` with proper
        polymorphic Jackson dispatch — and the round-trip survives
        when a Spring service re-publishes the spec via springdoc.

        ``flatten_inheritance=True`` overrides this and inlines all
        inherited properties at every concrete class (no ``allOf``).
        Useful for codegens that don't handle ``allOf`` well, or for
        producing a self-contained Swagger-UI-friendly view.

        Note: under standard JSON Schema ``allOf`` is intersection,
        so per-class enum pins on a discriminator field (Dataset's
        ``enum: [Dataset]``, Catalog's ``enum: [Catalog]``) intersect
        to ``∅``. That's a JSON Schema purity vs codegen ergonomics
        trade-off — Spring/openapi-generator doesn't run JSON Schema
        validation; it dispatches via the discriminator at the
        slot/path level and Jackson polymorphism, both of which work
        correctly. Strict-validator environments should switch to
        ``flatten_inheritance``.
        """
        if cls.is_a and not self.flatten_inheritance:
            parent_slot_names = set(self._induced_slots_by_name(cls.is_a))
            local_properties: dict[str, Schema | Reference] = {}
            local_required: list[str] = []
            for slot in self._induced_slots_iter(cls.name):
                if self._is_slot_excluded(slot):
                    continue
                self._record_rdf_slot_uri(cls.name, slot)
                if self._is_slot_body_excluded(cls, slot):
                    continue
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
            schema.description = self._class_description(cls)
            return schema

        # Flat schema: every induced slot (inherited and local) as a
        # top-level property. Used for non-inheriting classes and when
        # `flatten_inheritance` is on.
        properties: dict[str, Schema | Reference] = {}
        required: list[str] = []

        for slot in self._induced_slots_iter(cls.name):
            if self._is_slot_excluded(slot):
                continue
            self._record_rdf_slot_uri(cls.name, slot)
            if self._is_slot_body_excluded(cls, slot):
                continue
            properties[slot.name] = self._slot_to_schema(slot)
            if slot.required:
                required.append(slot.name)

        schema = Schema(type=DataType.OBJECT, additionalProperties=False)
        schema.title = cls.name
        schema.description = self._class_description(cls)
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
            ref = self._class_range_ref(slot, range_name)
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
        slot_description = self._slot_description(slot)
        if isinstance(base, Reference) and (slot_description or has_extras):
            # Wrap in allOf to add constraints alongside a $ref
            schema = Schema(allOf=[base])
            if slot_description:
                schema.description = slot_description
            return schema

        if isinstance(base, Schema):
            if slot_description:
                base.description = slot_description
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

        Cached on the instance for the duration of a build because both
        ``_collect_parent_chains`` and ``_build_openapi`` need it; the
        underlying walk is O(classes × slots).

        Profile filtering applies last: a class excluded by the active
        profile never gets endpoints, even if it carries
        ``openapi.resource: "true"``.
        """
        cached = getattr(self, "_resource_classes_cache", None)
        if cached is not None:
            return cached
        sv = self.schemaview
        excluded = self._excluded_classes

        if self.resource_filter:
            result = [c for c in self.resource_filter if c not in excluded]
        else:
            annotated = [
                name
                for name in sv.all_classes()
                if name not in excluded
                and _is_truthy(
                    self._class_annotation(sv.get_class(name), "openapi.resource") or False
                )
            ]
            if annotated:
                result = annotated
            else:
                result = [
                    name
                    for name in sv.all_classes()
                    if name not in excluded
                    and not sv.get_class(name).abstract
                    and not sv.get_class(name).mixin
                    and list(self._induced_slots_iter(name))
                ]
        self._resource_classes_cache = result
        return result

    def _induced_slots_by_name(self, class_name: str) -> dict[str, SlotDefinition]:
        """Per-build cache of induced slots indexed by slot name.

        The first call walks ``class_induced_slots(class_name)``;
        subsequent calls are an O(1) dict lookup. Used by
        ``_get_slot_annotation``, ``_render_slot_segment``, the
        nested-path emitters, and the chain builder — call sites that
        previously did a full linear scan per slot lookup, giving
        O(slots²) per class.

        The backing dict is also lazily allocated on first call so test
        paths that exercise helper methods directly (without going
        through ``serialize()``) still work.
        """
        cache = getattr(self, "_induced_slot_cache", None)
        if cache is None:
            cache = {}
            self._induced_slot_cache = cache
        cached = cache.get(class_name)
        if cached is None:
            cached = {s.name: s for s in self.schemaview.class_induced_slots(class_name)}
            cache[class_name] = cached
        return cached

    def _induced_slots_iter(self, class_name: str):
        """Cached iteration order matching ``class_induced_slots`` semantics."""
        return self._induced_slots_by_name(class_name).values()

    def _get_slot_annotation(self, cls: ClassDefinition, slot_name: str, tag: str) -> str | None:
        """Read a slot annotation, walking the same inheritance chain LinkML does.

        Resolution order (most specific wins):

        1. The class's direct ``slot_usage`` (fast path; same as today).
        2. The induced slot's annotations — this is where ``linkml-runtime``
           deposits the merged result of ``slot_usage`` from every ``is_a``
           ancestor, so a parent class's ``openapi.nested: "false"`` (and
           any other ``openapi.*`` annotation) reaches its subclasses
           automatically. Without this step, an annotation declared on a
           parent in slot_usage would silently fail to apply to children.
        3. The top-level slot definition (global default; same as today).
        """
        # 1. Direct slot_usage on the class.
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
        sv = self.schemaview
        # 2. Induced slot annotations — picks up slot_usage inherited from
        # ancestor classes through the is_a chain. Cached by class name
        # so this is an O(1) dict lookup.
        induced = self._induced_slots_by_name(cls.name).get(slot_name)
        if induced is not None:
            annotations = getattr(induced, "annotations", None)
            if annotations:
                # `class_induced_slots` returns SlotDefinition objects whose
                # `annotations` is a JsonObj — same dict-like access as a
                # regular Annotations container. Iterate keys defensively.
                keys = (
                    list(annotations.keys()) if hasattr(annotations, "keys") else list(annotations)
                )
                for key in keys:
                    ann = annotations[key]
                    if getattr(ann, "tag", None) == tag:
                        return str(ann.value)
        # 3. Top-level slot definition (global default).
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
        for slot in self._induced_slots_iter(cls.name):
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

    def _resolve_path_prefix(self) -> str:
        """Pick the effective URL path prefix for this build.

        Resolution order: CLI / Python ``path_prefix`` kwarg, then the
        schema-level ``openapi.path_prefix`` annotation, then no prefix.

        Normalisation: must start with ``/``; trailing ``/`` is stripped;
        no ``{…}`` placeholders (literal only — parameterised prefixes
        are a runtime / tenancy concern, not a spec one). Returns ``""``
        when no prefix is configured (caller treats that as no-op).
        """
        candidate = self.path_prefix
        if candidate is None:
            candidate = self._schema_annotation("openapi.path_prefix")
        if not candidate:
            return ""
        normalised = str(candidate).strip()
        if not normalised:
            return ""
        if not normalised.startswith("/"):
            raise ValueError(
                f"`openapi.path_prefix` {candidate!r} must start with `/` "
                "(use the absolute form, e.g. `/api/v1`)."
            )
        if "{" in normalised or "}" in normalised:
            raise ValueError(
                f"`openapi.path_prefix` {candidate!r} contains a `{{…}}` "
                "placeholder. Path prefixes must be literal — parameterised "
                "prefixes belong to runtime routing / API gateways."
            )
        # Strip a single trailing `/` (the prepend step adds back the
        # separator before each path key).
        if normalised != "/" and normalised.endswith("/"):
            normalised = normalised[:-1]
        return normalised

    def _apply_path_prefix(self, paths: dict[str, PathItem]) -> dict[str, PathItem]:
        """Prepend ``self._effective_path_prefix`` to every key in ``paths``.

        Validates: no key already starts with the prefix (would double
        up like ``/api/v1/api/v1/catalogs``). The most likely cause is
        a class-level ``openapi.path`` annotation that already includes
        the prefix — the error names the offending path so the schema
        author can pick one source.
        """
        prefix = self._effective_path_prefix
        prefixed: dict[str, PathItem] = {}
        for original_path, path_item in paths.items():
            # The doubled-prefix check fires when the class-level
            # ``openapi.path`` (or a user-authored ``openapi.path_template``)
            # already begins with the prefix. Either is fine on its own;
            # combining them silently is not.
            if original_path == prefix or original_path.startswith(prefix + "/"):
                raise ValueError(
                    f"Path {original_path!r} already starts with the "
                    f"configured path prefix {prefix!r}. Pick one source: "
                    "either drop the prefix from the class-level "
                    "`openapi.path` / `openapi.path_template` annotation, "
                    "or drop `--path-prefix` / `openapi.path_prefix`."
                )
            prefixed[f"{prefix}{original_path}"] = path_item
        return prefixed

    def _resolve_path_style(self) -> str:
        """Pick the effective URL path-segment convention for this build.

        Resolution order: CLI / Python `path_style` kwarg, then the
        schema-level `openapi.path_style` annotation, then the
        backward-compatible default `snake_case`. Unknown values raise
        with the supported list.
        """
        candidate = self.path_style
        if candidate is None:
            candidate = self._schema_annotation("openapi.path_style")
        if candidate is None:
            return PATH_STYLE_SNAKE
        normalised = str(candidate).strip().lower()
        if normalised not in SUPPORTED_PATH_STYLES:
            raise ValueError(
                f"Unsupported `openapi.path_style` {candidate!r}; "
                f"expected one of {sorted(SUPPORTED_PATH_STYLES)!r}."
            )
        return normalised

    def _apply_path_style(self, name: str) -> str:
        """Apply the active path-style to an auto-derived URL segment.

        ``snake_case`` returns the name unchanged (current behaviour);
        ``kebab-case`` swaps every ``_`` for ``-``. When the schema-level
        ``openapi.path_split_camel: "true"`` is set, also splits
        camelCase boundaries before applying the swap — e.g.
        ``inSeries`` → ``in-series``, ``contactPoint`` → ``contact-point``.
        Acronym-aware (``XMLParser`` → ``xml-parser``).

        Only operates on segments derived from identifier-shaped names
        (slot names, pluralised class names) — explicit overrides like
        ``openapi.path`` and ``openapi.path_segment`` are taken verbatim
        and never re-styled.

        Reads `_effective_path_style` directly; the attribute is set
        eagerly in ``serialize()`` for normal builds, and lazily by
        ``_resolve_path_style`` if a helper method is called before a
        full serialize (keeps test paths safe).
        """
        if getattr(self, "_effective_path_style", PATH_STYLE_SNAKE) != PATH_STYLE_KEBAB:
            return name
        if _is_truthy(self._schema_annotation("openapi.path_split_camel") or "false"):
            # Acronym-aware: "XMLParser" → "XML-Parser" → "xml-parser"
            name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
            name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name)
            return name.replace("_", "-").lower()
        return name.replace("_", "-")

    def _render_slot_segment(self, parent_cls: ClassDefinition | None, slot: SlotDefinition) -> str:
        """Render the URL segment for a slot used in a nested path.

        Honours the slot's `openapi.path_segment` annotation (verbatim)
        when set; otherwise applies the active path-style to the slot
        name. The slot identifier in the OpenAPI body, operation IDs,
        tags, and `x-rdf-property` extensions are untouched — only the
        URL segment changes.
        """
        if parent_cls is not None:
            override = self._get_slot_annotation(parent_cls, slot.name, "openapi.path_segment")
            if override:
                return override.strip()
        return self._apply_path_style(slot.name)

    def _get_path_segment(self, cls: ClassDefinition) -> str:
        """Get the URL path segment for a class.

        `openapi.path` is the explicit override (taken verbatim); when
        absent, the auto-derived snake-pluralised form is run through
        the active path-style so a schema-level
        ``openapi.path_style: "kebab-case"`` flips every auto-derived
        class segment in one place.
        """
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
        return self._apply_path_style(_to_path_segment(cls.name))

    def _get_operations(self, cls: ClassDefinition) -> list[str]:
        """Get the list of CRUD operations for a class."""
        ops = self._class_annotation(cls, "openapi.operations")
        if ops is None:
            return list(DEFAULT_OPERATIONS)
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
        """Walk the dumped spec and decorate schemas with x-rdf-* extensions.

        Emits both ``x-rdf-class`` (our existing custom name, what the
        spec-driven serdes runtime reads) and ``x-jsonld-type`` (the
        IETF draft-polli-restapi-ld-keywords-02 standard name). Both
        carry the same value — the class's expanded RDF IRI. Any
        consumer implementing the IETF draft picks up the standard
        keyword; our existing tooling continues to read the original.
        """
        schemas = (raw.get("components") or {}).get("schemas") or {}
        for class_name, schema in schemas.items():
            class_uri = self._x_rdf_class.get(class_name)
            if class_uri:
                schema["x-rdf-class"] = class_uri
                schema["x-jsonld-type"] = class_uri
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
                    if not isinstance(slot_schema, dict):
                        continue
                    slot_uri = self._x_rdf_property.get((class_name, slot_name))
                    if slot_uri:
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
        falls back to a synthesised RFC 7807 schema. The synthesised
        schema's name comes from ``openapi.error_class_name`` (e.g.
        ``"ProblemDetail"``) or defaults to ``"Problem"``.
        """
        if not self.error_schema:
            return None
        custom = self._schema_annotation("openapi.error_class")
        rename = self._schema_annotation("openapi.error_class_name")
        if custom is None:
            # Synthesised path: name comes from the rename annotation, else "Problem".
            return rename or "Problem"
        if rename is not None:
            warnings.warn(
                f"Both `openapi.error_class` ({custom!r}) and "
                f"`openapi.error_class_name` ({rename!r}) are set; the "
                "user-defined class wins and the rename is ignored. "
                "Drop one to silence this warning.",
                stacklevel=2,
            )
        if self.schemaview.get_class(custom) is None:
            raise ValueError(
                f"openapi.error_class refers to undefined class {custom!r}; "
                "add the class to the schema or remove the annotation."
            )
        return custom

    # --- Profiles --------------------------------------------------------

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
            for slot in self._induced_slots_iter(class_name):
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

    def _is_slot_body_excluded(self, cls: ClassDefinition, slot: SlotDefinition) -> bool:
        """True when the slot is excluded from the parent class's body schema.

        Honours ``openapi.body: "false"`` (#65) — slot generates the
        nested endpoint but is dropped from the parent's component
        ``properties``. Validates that the slot's range is a class
        (composition for routing only makes no sense for scalars), and
        that it is not also marked ``openapi.nested: "false"`` (the
        slot would have no representation at all).
        """
        body_ann = self._get_slot_annotation(cls, slot.name, "openapi.body")
        if body_ann is None or body_ann.strip().lower() != "false":
            return False
        if not slot.range or self.schemaview.get_class(slot.range) is None:
            raise ValueError(
                f'Slot {cls.name}.{slot.name!r} is annotated `openapi.body: "false"` '
                f"but its range {slot.range!r} is not a class. The annotation only "
                "makes sense on class-ranged slots — there's no nested endpoint to "
                "preserve otherwise."
            )
        nested_ann = self._get_slot_annotation(cls, slot.name, "openapi.nested")
        if nested_ann is not None and nested_ann.strip().lower() == "false":
            raise ValueError(
                f'Slot {cls.name}.{slot.name!r} is annotated both `openapi.body: "false"` '
                'and `openapi.nested: "false"`. The slot would have no representation '
                "at all — drop one annotation."
            )
        return True

    @staticmethod
    def _build_problem_schema(name: str = "Problem") -> Schema:
        """Construct the RFC 7807 Problem Details component schema.

        ``name`` becomes the schema's ``title``; callers pass the resolved
        ``openapi.error_class_name`` (e.g. ``"ProblemDetail"``).
        """
        schema = Schema(type=DataType.OBJECT, additionalProperties=True)
        schema.title = name
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

    # --- Discriminator / polymorphism -----------------------------------

    def _designates_type_slot(self, class_name: str) -> SlotDefinition | None:
        """Return the slot with `designates_type: true` on this class, or None."""
        for slot in self._induced_slots_iter(class_name):
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

    def _class_response_ref(self, class_name: str) -> Schema | Reference:
        """Schema or ``$ref`` for the ``class_name`` payload in a path op.

        When the class has 2+ concrete descendants (it's polymorphic),
        emit ``oneOf: [$ref each concrete option]`` plus the inherited
        ``discriminator`` so a GET on /datasets/{id} can legitimately
        return a ``DatasetSeries`` and the spec advertises it. This is
        what openapi-generator's Spring template needs to wire up
        Jackson polymorphic deserialization on the response side.

        Used uniformly across list, read, create, update, patch, and
        nested-collection responses — any place a class is the body
        shape of a path operation.
        """
        sv = self.schemaview
        descendants = self._concrete_descendants_including_self(class_name)
        if len(descendants) <= 1:
            return Reference(ref=f"#/components/schemas/{class_name}")
        oneof = [Reference(ref=f"#/components/schemas/{n}") for n in descendants]
        schema = Schema(oneOf=oneof)
        field = self._inherited_discriminator_field(class_name)
        if field is not None:
            mapping = {
                self._type_value(sv.get_class(n)): f"#/components/schemas/{n}" for n in descendants
            }
            schema.discriminator = Discriminator(propertyName=field, mapping=mapping)
        return schema

    def _validate_inlined_recursion(self) -> None:
        """Reject inlined-composition cycles unless explicitly acknowledged.

        An inlined cycle (e.g., a class with an ``inlined: true`` slot whose
        range transitively reaches the class itself) inflates JSON payloads
        infinitely and makes most codegens spin during DTO generation. The
        author has to make an explicit choice:

          * drop ``inlined: true`` on the offending slot so it becomes a
            reference (the default for identifier-bearing classes —
            target's IRI is sent on the wire), OR
          * annotate any class on the cycle with
            ``openapi.recurse_max_depth: <int>`` to acknowledge the cycle
            is intentional. The annotation is opt-in; the integer is
            currently informational (no depth bound is enforced) — the
            point is to make the failure loud and deliberate, not silent.

        Reference cycles (``inlined: false``) are fine: they're IRI
        strings on the wire, no expansion happens.
        """
        sv = self.schemaview

        # Build the composition adjacency:  class → [(slot, range), ...]
        composition: dict[str, list[tuple[str, str]]] = {}
        for class_name in sv.all_classes():
            edges: list[tuple[str, str]] = []
            for slot in self._induced_slots_iter(class_name):
                if slot.range and sv.get_class(slot.range) and self._is_composition(slot):
                    edges.append((slot.name, slot.range))
            if edges:
                composition[class_name] = edges

        if not composition:
            return

        # 3-color DFS for back-edge detection. ``stack`` records the chain
        # of edges (parent, slot, child) currently being visited; on a back
        # edge to a node already on the stack we extract the cycle for
        # error reporting.
        in_stack: dict[str, int] = {}
        visited: set[str] = set()
        edge_stack: list[tuple[str, str, str]] = []

        def find_cycle(node: str) -> list[tuple[str, str, str]] | None:
            in_stack[node] = len(edge_stack)
            for slot_name, target in composition.get(node, []):
                if target in in_stack:
                    return edge_stack[in_stack[target] :] + [(node, slot_name, target)]
                if target not in visited and target in composition:
                    edge_stack.append((node, slot_name, target))
                    cycle = find_cycle(target)
                    edge_stack.pop()
                    if cycle is not None:
                        return cycle
            del in_stack[node]
            visited.add(node)
            return None

        for class_name in composition:
            if class_name in visited:
                continue
            cycle = find_cycle(class_name)
            if cycle is None:
                continue
            cycle_classes = {parent for parent, _, _ in cycle} | {child for _, _, child in cycle}
            if any(
                self._class_annotation(sv.get_class(n), "openapi.recurse_max_depth") is not None
                for n in cycle_classes
            ):
                continue
            path = (
                " -> ".join(f"{parent}.{slot}" for parent, slot, _ in cycle) + f" -> {cycle[-1][2]}"
            )
            raise ValueError(
                f"Inlined composition cycle detected: {path}. Inlined cycles "
                f"cause infinite expansion in generated JSON payloads. "
                f"Resolve by setting `inlined: false` on one of the slots in "
                f"the cycle (the on-the-wire shape becomes a reference IRI), "
                f"or by adding `openapi.recurse_max_depth: <N>` as a class "
                f"annotation on a class in the cycle to acknowledge it is "
                f"intentional."
            )

    def _is_in_polymorphic_chain(self, cls: ClassDefinition) -> bool:
        """True if ``cls`` or any ancestor declares a discriminator.

        Drives the flatten-vs-allOf decision in :meth:`_class_to_schema`:
        polymorphic chains flatten so each concrete schema can pin its
        own discriminator without ``allOf`` intersection conflicts.
        """
        sv = self.schemaview
        cur: ClassDefinition | None = cls
        while cur is not None:
            if self._discriminator_field(cur) is not None:
                return True
            cur = sv.get_class(cur.is_a) if cur.is_a else None
        return False

    def _inherited_discriminator_field(self, class_name: str) -> str | None:
        """Walk the ``is_a`` chain looking for a discriminator declaration.

        ``_discriminator_field`` only inspects a single class — it does
        not see annotations on ancestors. For property-level polymorphism
        (e.g., a slot ranging on ``Dataset`` when the discriminator is
        declared on ``Resource``) we need the chain walk so the
        property's ``oneOf`` can carry the same ``discriminator``
        block the schema-level emission already uses.
        """
        sv = self.schemaview
        cls = sv.get_class(class_name)
        while cls is not None:
            field = self._discriminator_field(cls)
            if field is not None:
                return field
            cls = sv.get_class(cls.is_a) if cls.is_a else None
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

    def _concrete_descendants_excluding_self(self, class_name: str) -> list[str]:
        """Concrete (non-abstract, non-mixin) descendants — never the root itself.

        The discriminator root is the union *category*; subclasses are the
        addressable *instances*. Whether the root is marked
        ``abstract: true`` doesn't matter for this rule — the discriminator
        semantics make it the union root by definition, so it must not
        appear in its own ``oneOf`` array or ``discriminator.mapping``
        (downstream codegens read the self-reference as cyclic inheritance).

        Mixins are excluded entirely from polymorphic mappings — they're
        trait composition, not subtyping.

        Cached per build because ``_class_response_ref`` calls this
        for every operation on every resource class (5+ calls per
        class). Without caching, ``sv.class_descendants`` re-walks the
        full class graph on every call.
        """
        cache = getattr(self, "_concrete_descendants_cache", None)
        if cache is None:
            cache = {}
            self._concrete_descendants_cache = cache
        if class_name in cache:
            return cache[class_name]
        sv = self.schemaview
        out: list[str] = []
        for name in sv.class_descendants(class_name, reflexive=False):
            cls = sv.get_class(name)
            if cls is None or cls.abstract or cls.mixin:
                continue
            out.append(name)
        cache[class_name] = out
        return out

    def _concrete_descendants_including_self(self, class_name: str) -> list[str]:
        """Concrete descendants prepended with the root when concrete.

        Used at *use sites* (path responses, slot ranges) where the
        polymorphic union must include the root itself as one of the
        addressable types — opposite of the discriminator-root case
        handled by :meth:`_concrete_descendants_excluding_self`.
        """
        descendants = self._concrete_descendants_excluding_self(class_name)
        cls = self.schemaview.get_class(class_name)
        if cls is None or cls.abstract or cls.mixin:
            return descendants
        return [class_name] + descendants

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
            concrete = self._concrete_descendants_excluding_self(class_name)
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

            for tv, sub_name in seen.items():
                self._inject_subclass_type_value(schemas, sub_name, field, tv)

            # Optional back-compat field: `openapi.legacy_type_field` on
            # the polymorphic root names a per-class constant property
            # (e.g. `#type` carrying a Java FQN like
            # `com.xyz.dcat.Catalog`). The value comes from each
            # concrete class's `openapi.legacy_type_value`. Coexists
            # with the proper discriminator — codegen uses the latter
            # for polymorphic dispatch; the former is just a stable
            # opaque marker for legacy consumers.
            #
            # ``openapi.legacy_type_codegen_name`` (optional) — supplies
            # a clean target-language identifier via ``x-codegen-name``
            # so openapi-generator's Java/Spring/TS templates produce
            # ``private String legacyType`` with ``@JsonProperty("#type")``
            # rather than the auto-mangled ``hashType``/``atType`` form.
            legacy_field = self._class_annotation(cls, "openapi.legacy_type_field")
            if legacy_field is not None:
                legacy_field = legacy_field.strip()
                codegen_name = self._class_annotation(cls, "openapi.legacy_type_codegen_name")
                codegen_name = codegen_name.strip() if codegen_name else None
                for sub_name in seen.values():
                    self._inject_legacy_type_value(schemas, sub_name, legacy_field, codegen_name)

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
        """Pin the discriminator field on a concrete class to its single
        type value: ``enum: [<self>], default: <self>``.

        Every concrete class — leaf or intermediate — gets the same
        treatment. A ``Dataset`` payload always carries
        ``resourceType: "Dataset"``; a ``Catalog`` payload always
        carries ``resourceType: "Catalog"``. That's what the wire
        format means in DCAT-3 (and in JSON-LD ``@type`` aliasing
        generally), and it's what openapi-generator's Spring template
        wires through Jackson's ``@JsonTypeInfo`` /
        ``@JsonSubTypes`` so each generated DTO has a final
        ``resourceType`` constant.

        Trade-off — JSON Schema purity vs codegen ergonomics: under
        strict ``allOf``-as-intersection semantics, ``Catalog``
        inherits ``Dataset``'s ``enum: [Dataset]`` via its
        ``allOf[0] = {$ref: Dataset}`` and intersects with its own
        ``enum: [Catalog]`` to ``∅``. A pure JSON Schema validator
        would reject every ``Catalog`` payload. Spring codegen
        doesn't run JSON Schema validation — it dispatches via the
        discriminator at the slot/path level (``oneOf`` + ``mapping``)
        and relies on Jackson polymorphic deserialization, both of
        which work correctly here. The per-class pin is what makes
        the DTOs and Swagger UI display say what they should: every
        concrete class has a fixed ``resourceType``.
        """
        schema = schemas.get(class_name)
        if not isinstance(schema, Schema):
            return
        local = self._writable_local_schema(schema)
        properties = dict(local.properties or {})
        disc = Schema(
            type=DataType.STRING,
            enum=[type_value],
            default=type_value,
        )
        properties[field] = disc
        local.properties = properties
        # The discriminator's RDF identity belongs to the *class*, not
        # to a wire-format field. Class-level ``x-rdf-class`` (graph
        # emission) and ``x-jsonld-type`` (IETF JSON-LD alignment)
        # already convey it; attaching ``x-rdf-property: rdf:type`` to
        # this field would tempt RDF runtimes to emit a malformed
        # ``<subject> rdf:type "Dataset"`` literal triple.
        required = list(local.required or [])
        if field not in required:
            required.append(field)
        local.required = required

    def _inject_legacy_type_value(
        self,
        schemas: dict[str, Schema | Reference],
        class_name: str,
        field: str,
        codegen_name: str | None = None,
    ) -> None:
        """Inject an opaque per-class constant for back-compat consumers.

        The value comes from ``openapi.legacy_type_value`` on the
        concrete class — required when the polymorphic root declared
        ``openapi.legacy_type_field``. Format is up to the author
        (Java FQN, custom IRI, anything stable). No RDF mapping is
        attached: this is a local convention, not a real predicate.
        Errors loudly if a concrete class is missing the value, since
        silent omission would leave gaps in the wire payload.

        ``codegen_name`` (optional) is recorded on the generator
        instance for :meth:`emit_name_mappings` to dump to a sibling
        ``name-mappings.txt`` file the user passes to
        ``openapi-generator-cli --name-mappings @<file>``. That CLI
        option is the reliable way to rename an awkward JSON property
        (``#type``) onto a clean target-language identifier
        (``legacyType``) while preserving the wire name via
        ``@JsonProperty``. The spec itself stays unannotated —
        openapi-generator's Java/Spring/TS templates do **not** have
        a universal property-renaming vendor extension, despite some
        documentation suggesting otherwise.
        """
        sv = self.schemaview
        cls = sv.get_class(class_name)
        if cls is None:
            return
        value = self._class_annotation(cls, "openapi.legacy_type_value")
        if value is None:
            raise ValueError(
                f"Class {class_name!r} is on a polymorphic chain that declares "
                f"`openapi.legacy_type_field: {field!r}` but does not set "
                f"`openapi.legacy_type_value` on this class. Each concrete "
                f"class in the chain must specify the legacy type value "
                f"explicitly — the format is up to you (Java FQN, custom "
                f"IRI, etc.) and there is deliberately no fallback."
            )
        value = value.strip()
        schema = schemas.get(class_name)
        if not isinstance(schema, Schema):
            return
        local = self._writable_local_schema(schema)
        properties = dict(local.properties or {})
        properties[field] = Schema(
            type=DataType.STRING,
            enum=[value],
            default=value,
        )
        local.properties = properties
        required = list(local.required or [])
        if field not in required:
            required.append(field)
        local.required = required
        if codegen_name:
            self._codegen_name_mappings[field] = codegen_name

    def _make_list_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        media_types = self._get_media_types(cls)
        array_schema = Schema(
            type=DataType.ARRAY,
            items=self._class_response_ref(class_name),
        )
        return Operation(
            summary=f"List {_to_path_segment(class_name).replace('_', ' ')}",
            operationId=f"list_{_to_path_segment(class_name)}",
            tags=[self._class_tag(class_name)],
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
        ref = self._class_response_ref(class_name)
        return Operation(
            summary=f"Create a {class_name}",
            operationId=f"create_{_to_snake_case(class_name)}",
            tags=[self._class_tag(class_name)],
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
        ref = self._class_response_ref(class_name)
        return Operation(
            summary=f"Get a {class_name}",
            operationId=f"get_{_to_snake_case(class_name)}",
            tags=[self._class_tag(class_name)],
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
        ref = self._class_response_ref(class_name)
        return Operation(
            summary=f"Update a {class_name}",
            operationId=f"update_{_to_snake_case(class_name)}",
            tags=[self._class_tag(class_name)],
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
            tags=[self._class_tag(class_name)],
            responses={
                "204": Response(description=f"{class_name} deleted"),
                "404": self._error_response("Not found"),
            },
        )

    # --- PATCH operations ------------------------------------------------

    def _make_patch_operation(self, cls: ClassDefinition, class_name: str) -> Operation:
        """Emit a PATCH item operation using JSON Merge Patch (RFC 7396).

        Request body media type is fixed at ``application/merge-patch+json``
        — RFC 7396 is JSON-specific, so the class's ``openapi.media_types``
        do not apply to the request side. The 200 response uses the full
        class schema and honours ``openapi.media_types`` as usual.
        """
        media_types = self._get_media_types(cls)
        full_ref = self._class_response_ref(class_name)
        patch_ref = Reference(ref=f"#/components/schemas/{class_name}Patch")
        return Operation(
            summary=f"Patch a {class_name}",
            operationId=f"patch_{_to_snake_case(class_name)}",
            tags=[self._class_tag(class_name)],
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
        for slot in self._induced_slots_iter(class_name):
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

    # --- Composition vs reference ---------------------------------------

    def _validate_resource_addressability(
        self, class_name: str, path_vars: list, operations: list[str]
    ) -> None:
        """Resource classes that need item-path operations must be addressable.

        Item-path ops (read / update / delete) require either an identifier
        slot or an explicit ``openapi.path_variable`` annotation. If neither
        is present the class can't be referenced individually and the
        generator should fail loudly rather than silently drop the item path.
        """
        item_ops = ITEM_OPERATIONS & set(operations) - {OP_PATCH}
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
        for slot in self._induced_slots_iter(class_name):
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

    def _class_range_ref(self, slot: SlotDefinition, range_name: str) -> Schema | Reference:
        """Build the schema or ``$ref`` for a class- or enum-ranged slot.

        Three branches, in priority order:

        1. **Reference** (``inlined: false`` against an identifier-bearing
           class) → plain IRI string (``type: string, format: uri``).
           RDF-roundtrippable: the same value is valid when a content
           negotiation layer renders ``application/ld+json`` or
           ``text/turtle`` from the same response.

        2. **Polymorphic composition** (``inlined: true``, range has 2+
           concrete descendants) → ``oneOf: [$ref each concrete option]``
           at the property level, carrying the inherited
           ``discriminator`` + ``mapping`` if one exists in the chain.
           This is the shape openapi-generator's Spring template wires
           to Jackson ``@JsonSubTypes`` for polymorphic deserialization.
           The schema-level ``discriminator`` + ``oneOf`` on the
           polymorphic root remains; this property-level emission gives
           codegens the join-point information they need at the slot.

        3. **Plain composition** (everything else) → ``$ref <Range>``.

        Enum ranges always go through branch 3.
        """
        sv = self.schemaview
        target_cls = sv.get_class(range_name)

        if (
            target_cls is not None
            and not self._is_composition(slot)
            and self._identifier_slot(range_name) is not None
        ):
            return Schema(type=DataType.STRING, schema_format="uri")

        if target_cls is not None and self._is_composition(slot):
            descendants = self._concrete_descendants_including_self(range_name)
            if len(descendants) > 1:
                oneof = [Reference(ref=f"#/components/schemas/{n}") for n in descendants]
                schema = Schema(oneOf=oneof)
                field = self._inherited_discriminator_field(range_name)
                if field is not None:
                    mapping = {
                        self._type_value(sv.get_class(n)): f"#/components/schemas/{n}"
                        for n in descendants
                    }
                    schema.discriminator = Discriminator(propertyName=field, mapping=mapping)
                return schema

        return Reference(ref=f"#/components/schemas/{range_name}")

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

    def _class_tag(self, class_name: str) -> str:
        """OpenAPI ``tags`` value to use for a class's operations.

        Defaults to the class name (current behaviour); the
        ``openapi.tag`` class annotation overrides it. Composition- and
        reference-derived nested operations call this with the *target*
        class so all "Dataset" operations end up under one Swagger UI
        group regardless of where in the URL hierarchy they emit.
        """
        cls = self.schemaview.get_class(class_name)
        override = self._class_annotation(cls, "openapi.tag") if cls else None
        if override:
            return override.strip()
        return class_name

    def _resolve_item_path_vars(
        self,
        class_name: str,
        path_vars: list,
    ) -> tuple[str, list[Parameter]]:
        """Render a class's path variables into a URL suffix and Parameter list.

        Single source of truth for the ``openapi.path_id`` rename: when
        the class has exactly one path variable *and* declares
        ``openapi.path_id``, the override renames the URL parameter (and
        the matching ``Parameter.name``) so the same identifier shows up
        consistently in the flat item path, in nested-paths-from-this-
        class, and in any deep chain that passes through this class as an
        ancestor. Without the annotation the URL parameter falls back to
        the slot name (preserving byte-identical output for existing
        schemas).
        """
        cls = self.schemaview.get_class(class_name)
        path_id_override: str | None = None
        if len(path_vars) == 1 and cls is not None:
            raw = self._class_annotation(cls, "openapi.path_id")
            path_id_override = raw.strip() if raw else None
        names = [path_id_override or slot.name for slot, _mode in path_vars]
        item_suffix = "/".join(f"{{{name}}}" for name in names)
        path_params = [
            Parameter(
                name=name,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._path_variable_schema(slot, mode),
            )
            for name, (slot, mode) in zip(names, path_vars)
        ]
        return item_suffix, path_params

    def _attach_item_operations(
        self,
        item: PathItem,
        cls: ClassDefinition,
        class_name: str,
        operations: list[str],
    ) -> None:
        """Attach `read` / `update` / `patch` / `delete` operations to a PathItem.

        Single source of truth for the conditional CRUD-attach block,
        previously duplicated across ``_build_openapi``,
        ``_emit_chained_deep_path``, and ``_emit_templated_deep_path``.
        """
        if OP_READ in operations:
            item.get = self._make_read_operation(cls, class_name)
        if OP_UPDATE in operations:
            item.put = self._make_update_operation(cls, class_name)
        if OP_PATCH in operations:
            item.patch = self._make_patch_operation(cls, class_name)
        if OP_DELETE in operations:
            item.delete = self._make_delete_operation(cls, class_name)

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
        into a URL prefix and the matching parameter list (via the same
        ``openapi.path_id`` resolution used by the flat item path), then
        delegates to :meth:`_make_nested_paths_with_prefix` so single-level
        and N-level emission share the same code path.
        """
        var_suffix, parent_path_params = self._resolve_item_path_vars(
            parent_class_name, parent_path_vars
        )
        prefix = f"{parent_path_segment}/{var_suffix}" if var_suffix else parent_path_segment
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

        for slot in self._induced_slots_iter(parent_class_name):
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

            collection_path = f"/{url_prefix}/{self._render_slot_segment(parent_cls, slot)}"
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
            # Synthetic inverses have no real slot to look up an
            # `openapi.path_segment` annotation on, so we just apply the
            # active path-style to the synthesised name.
            collection_path = f"/{url_prefix}/{self._apply_path_style(synth_name)}"
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
            for slot in self._induced_slots_iter(src_class_name):
                if not slot.inverse or "." not in str(slot.inverse):
                    continue
                tgt_class, tgt_slot_name = str(slot.inverse).split(".", 1)
                if tgt_slot_name in self._excluded_slots:
                    continue
                if tgt_class not in target_slot_names_cache:
                    target_slot_names_cache[tgt_class] = set(self._induced_slots_by_name(tgt_class))
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
        return build_parent_chains_index(
            self.schemaview,
            resource_classes=set(self._get_resource_classes()),
            excluded_classes=self._excluded_classes,
            is_slot_excluded=self._is_slot_excluded,
            get_slot_annotation=self._get_slot_annotation,
            induced_slots=lambda name: list(self._induced_slots_iter(name)),
        )

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

    def _canonical_parent_chain(self, class_name: str) -> list[tuple[str, str]]:
        cls = self.schemaview.get_class(class_name)
        annotated = self._class_annotation(cls, "openapi.parent_path") if cls else None
        return canonical_parent_chain(class_name, self._parent_chains_index, annotated)

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
            parent_cls = sv.get_class(parent_name)
            slot_def = self._induced_slots_by_name(parent_name).get(slot_name)
            slot_segment = (
                self._render_slot_segment(parent_cls, slot_def)
                if slot_def is not None
                else self._apply_path_style(slot_name)
            )
            if i == 0:
                parent_segment = self._get_path_segment(parent_cls)
                prefix_parts.append(f"{parent_segment}/{{{param_name}}}/{slot_segment}")
            else:
                prefix_parts.append(f"{{{param_name}}}/{slot_segment}")
            params.append(
                Parameter(
                    name=param_name,
                    param_in=ParameterLocation.PATH,
                    required=True,
                    param_schema=self._slot_to_schema(id_slot),
                )
            )
        return "/".join(prefix_parts), params

    def _emit_chained_deep_path(
        self,
        cls: ClassDefinition,
        class_name: str,
        chain: list[tuple[str, str]],
        item_suffix: str,
        path_params: list[Parameter],
        operations: list[str],
    ) -> dict[str, PathItem]:
        """Emit the auto-derived deep item path for a class with a parent chain.

        The deep URL is the chain prefix (already ending in the slot that
        leads to the leaf class) plus the leaf's own ``item_suffix`` —
        e.g. ``/catalogs/{catalogId}/datasets/{datasetId}/distributions/{distId}``.
        Operation IDs are suffixed ``_via_<chain>`` so they remain globally
        unique alongside the leaf's flat-path operations.
        """
        chain_prefix, chain_params = self._build_chain_path_params(chain)
        deep_item_path = f"/{chain_prefix}/{item_suffix}"
        deep_item = PathItem(parameters=list(chain_params) + path_params)
        self._attach_item_operations(deep_item, cls, class_name, operations)
        chain_suffix = "_via_" + "_".join(_to_snake_case(p) for p, _ in chain)
        deep_paths = {deep_item_path: deep_item}
        self._suffix_operation_ids(deep_paths, chain_suffix)
        # Re-tag deep-chain operations to the *immediate URL parent* (the
        # last chain hop's class) so Swagger UI groups them with the rest
        # of the parent's nested ops rather than under the leaf's tag
        # (#68). The leaf's flat-path operations keep the leaf's tag.
        if chain:
            parent_tag = self._class_tag(chain[-1][0])
            self._retag_path_operations(deep_paths, parent_tag)
        return deep_paths

    @staticmethod
    def _retag_path_operations(paths: dict[str, PathItem], tag: str) -> None:
        """Overwrite every operation's ``tags`` to ``[tag]``."""
        for path_item in paths.values():
            for method in ("get", "put", "post", "patch", "delete"):
                op = getattr(path_item, method, None)
                if op is not None:
                    op.tags = [tag]

    _PATH_TEMPLATE_PLACEHOLDER_RE = PATH_TEMPLATE_PLACEHOLDER_RE

    @staticmethod
    def _parse_path_param_sources(class_name: str, raw: str) -> dict[str, tuple[str, str]]:
        return _parse_path_param_sources_helper(class_name, raw)

    def _emit_templated_deep_path(
        self,
        cls: ClassDefinition,
        class_name: str,
        template: str,
        operations: list[str],
    ) -> dict[str, PathItem]:
        """Emit a deep item path (and optional collection) from a literal `openapi.path_template`.

        The user-supplied template replaces the auto-derived chain. Each
        ``{name}`` placeholder must have a matching ``name:Class.slot``
        entry in ``openapi.path_param_sources``; the slot's range drives
        the parameter schema (so typed parameters and RDF metadata still
        flow). Validates: placeholder set matches source-key set,
        every ``Class.slot`` resolves, no duplicates.

        When the template ends with a ``/{name}`` segment, the collection
        path (template minus that tail) is emitted by default with
        ``list`` / ``create`` operations from ``operations``. Opt out
        with ``openapi.path_template_collection: "false"`` for legacy
        item-only URLs.
        """
        sv = self.schemaview
        sources_raw = self._class_annotation(cls, "openapi.path_param_sources") or ""
        sources = self._parse_path_param_sources(class_name, sources_raw)
        placeholders = list(self._PATH_TEMPLATE_PLACEHOLDER_RE.findall(template))
        unique_placeholders = list(dict.fromkeys(placeholders))  # preserve order, dedupe

        if len(unique_placeholders) != len(placeholders):
            duplicates = sorted({p for p in placeholders if placeholders.count(p) > 1})
            raise ValueError(
                f"Class {class_name!r} `openapi.path_template` "
                f"{template!r} repeats placeholder(s) {duplicates}: "
                "OpenAPI requires unique parameter names per path."
            )

        missing_sources = set(unique_placeholders) - set(sources)
        extra_sources = set(sources) - set(unique_placeholders)
        if missing_sources or extra_sources:
            raise ValueError(
                f"Class {class_name!r} `openapi.path_template` placeholders "
                f"don't match `openapi.path_param_sources`. "
                f"Template placeholders: {sorted(unique_placeholders)!r}. "
                f"Source keys: {sorted(sources)!r}. "
                f"Missing sources: {sorted(missing_sources)!r}. "
                f"Extra sources (not in template): {sorted(extra_sources)!r}."
            )

        # Build a parameter for each placeholder, indexed by name so the
        # collection-path emission can drop the leaf-id one.
        params_by_name: dict[str, Parameter] = {}
        for name in unique_placeholders:
            src_class, src_slot = sources[name]
            if sv.get_class(src_class) is None:
                raise ValueError(
                    f"Class {class_name!r} `openapi.path_param_sources` "
                    f"refers to unknown class {src_class!r} for "
                    f"parameter {name!r}."
                )
            slot = self._induced_slots_by_name(src_class).get(src_slot)
            if slot is None:
                raise ValueError(
                    f"Class {class_name!r} `openapi.path_param_sources` "
                    f"refers to unknown slot {src_class}.{src_slot!r} for "
                    f"parameter {name!r}."
                )
            params_by_name[name] = Parameter(
                name=name,
                param_in=ParameterLocation.PATH,
                required=True,
                param_schema=self._slot_to_schema(slot),
            )

        deep_item = PathItem(parameters=[params_by_name[n] for n in unique_placeholders])
        self._attach_item_operations(deep_item, cls, class_name, operations)
        deep_paths: dict[str, PathItem] = {template: deep_item}

        # Collection path: default-on when the template ends with a
        # placeholder segment AND the class declares any of `list` /
        # `create` in its operations. Opt-out with
        # `openapi.path_template_collection: "false"`.
        collection_opt_in = self._class_annotation(cls, "openapi.path_template_collection")
        emit_collection = collection_opt_in is None or _is_truthy(collection_opt_in)
        if emit_collection and template.endswith("}"):
            # Strip the trailing `/{name}` segment.
            tail_open = template.rfind("/{")
            tail_name = template[tail_open + 2 : -1] if tail_open != -1 else None
            if tail_name and tail_name in params_by_name:
                collection_path = template[:tail_open]
                if collection_path:  # don't emit the empty-prefix degenerate case
                    collection_params = [
                        params_by_name[n] for n in unique_placeholders if n != tail_name
                    ]
                    collection = PathItem(
                        parameters=list(collection_params) if collection_params else None
                    )
                    if OP_LIST in operations:
                        collection.get = self._make_list_operation(cls, class_name)
                    if OP_CREATE in operations:
                        collection.post = self._make_create_operation(cls, class_name)
                    if collection.get is not None or collection.post is not None:
                        deep_paths[collection_path] = collection

        self._suffix_operation_ids(deep_paths, "_via_template")
        return deep_paths

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
        # Cycle protection — must come before any emission. Mutual
        # composition (`A.bs[B].as[A]`) would otherwise recurse one
        # extra hop and emit paths whose URL parameters repeat (e.g.
        # `/as/{id}/bs/{b_id}/as/{a_id}/bs/{b_id}`). The stack is
        # populated by the recursion below, so the *second* visit to
        # an already-being-emitted target short-circuits before any
        # paths are added.
        if target_class_name in self._composition_emission_stack:
            return
        target_cls = self.schemaview.get_class(target_class_name)
        media_types = self._get_media_types(target_cls)
        target_ref = self._class_response_ref(target_class_name)
        array_schema = Schema(type=DataType.ARRAY, items=target_ref)
        # Nested composition operations group under the *parent* class's
        # tag (Swagger UI groups by tag). All operations under
        # /<parent>/{id}/... live with the rest of the parent's surface
        # rather than scattering across the child's tag (#68).
        op_tag = self._class_tag(parent_class_name)
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

        # Multi-level composition: when the composed target itself has
        # `inlined: true` slots, recurse and emit deeper paths under
        # this item URL. The cycle stack (checked at the top of this
        # method) terminates mutual-composition graphs; references
        # aren't recursed into because they already address the target
        # as its own resource at the top level.
        self._composition_emission_stack.add(target_class_name)
        try:
            self._recurse_composition_children(
                paths, target_class_name, item_path, item_path_params
            )
        finally:
            self._composition_emission_stack.discard(target_class_name)

    def _recurse_composition_children(
        self,
        paths: dict,
        target_class_name: str,
        item_path: str,
        item_path_params: list[Parameter],
    ) -> None:
        """Walk a composition target's own composition slots and emit deeper paths.

        Only ``inlined: true`` (composition) children are recursed —
        reference-shaped children address the target as its own
        resource at the top level and don't need a deeper URL prefix.
        Honours ``openapi.nested: "false"`` and profile exclusions.
        """
        sv = self.schemaview
        target_cls = sv.get_class(target_class_name)
        if target_cls is None:
            return
        for child_slot in self._induced_slots_iter(target_class_name):
            if not child_slot.multivalued:
                continue
            if self._is_slot_excluded(child_slot):
                continue
            child_target = child_slot.range
            if not child_target or sv.get_class(child_target) is None:
                continue
            nested_ann = self._get_slot_annotation(target_cls, child_slot.name, "openapi.nested")
            if nested_ann is not None and not _is_truthy(nested_ann):
                continue
            if not self._is_composition(child_slot):
                continue
            child_target_id = self._identifier_slot(child_target)
            child_collection_path = (
                f"{item_path}/{self._render_slot_segment(target_cls, child_slot)}"
            )
            self._add_composition_paths(
                paths,
                child_collection_path,
                target_class_name,
                child_slot,
                child_target,
                child_target_id,
                item_path_params,
            )

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
        target_ref = self._class_response_ref(target_class_name)
        array_schema = Schema(type=DataType.ARRAY, items=target_ref)

        link_ref = Reference(ref="#/components/schemas/ResourceLink")
        # Body accepts a single link or a batch — clients prefer batch.
        link_body_schema = Schema(oneOf=[link_ref, Schema(type=DataType.ARRAY, items=link_ref)])
        # Reference attach/detach operations group under the *parent*
        # class's tag for the same reason as composition (#68).
        op_tag = self._class_tag(parent_class_name)
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
