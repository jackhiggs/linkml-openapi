"""LinkML → Spring server source emitter.

Walks a LinkML schema and writes Java source files under a target
directory:

* ``<package>/model/*.java`` — DTOs with Jackson polymorphism
  annotations on roots, ``extends`` for subclass chains, JsonProperty
  for awkward wire names like ``#type``.
* ``<package>/api/*.java`` — Spring ``@RestController`` interfaces
  with default 501 method bodies for each top-level resource.

Polymorphism story: classes carrying ``openapi.discriminator`` (or with
an ancestor that does) become a Java inheritance chain. The polymorphic
root is annotated ``@JsonTypeInfo(NAME, propertyName=<discriminator>)``
plus ``@JsonSubTypes(...)`` enumerating concrete leaves. Each concrete
class declares the discriminator field as a ``final`` default so the
DTO constructs with the right tag.

Scope (MVP):
* top-level resources (classes with ``openapi.resource: "true"``) get
  controller interfaces; nested CRUD paths are out of scope here
* embedded composition slots (no identifier on target) → ``$ref`` to
  the embedded class; reference slots (target has identifier,
  ``inlined: false``) → URI string
* skip multivalued nested CRUD, attach/detach paths, query parameters,
  patch — those land later as the emitter matures
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView

# Shared helper imported from the OpenAPI generator so the two
# emitters produce identical snake-case path segments for the same
# class names — divergent regexes (e.g. for ``HTMLParser``) here
# would silently produce mismatched URLs between the spec and Spring
# routes.
from linkml_openapi._chains import (
    ChainHop,
    build_parent_chains_index,
    canonical_parent_chain,
    render_chain_hops,
)
from linkml_openapi._query_params import QueryParamSpec, walk_query_params
from linkml_openapi.generator import _to_snake_case


_TEMPLATES_DIR = Path(__file__).parent / "templates"


_RANGE_TYPE_MAP: dict[str, tuple[str, str | None]] = {
    # (java_type, import_path or None)
    "string": ("String", None),
    "integer": ("Long", None),
    "float": ("Float", None),
    "double": ("Double", None),
    "decimal": ("java.math.BigDecimal", "java.math.BigDecimal"),
    "boolean": ("Boolean", None),
    "date": ("java.time.LocalDate", "java.time.LocalDate"),
    "datetime": ("java.time.OffsetDateTime", "java.time.OffsetDateTime"),
    # RDF-style links: URI / URIORCURIE / nodeidentifier all map to
    # java.net.URI so the Java type announces "this is an RDF link".
    # Jackson serialises URI as a bare quoted string, and springdoc
    # round-trips it as ``type: string, format: uri`` — wire shape
    # unchanged from the OpenAPI side, semantics richer in code.
    "uri": ("java.net.URI", "java.net.URI"),
    "uriorcurie": ("java.net.URI", "java.net.URI"),
    "nodeidentifier": ("java.net.URI", "java.net.URI"),
}


@dataclass
class SpringServerGenerator:
    """LinkML → Spring server source emitter.

    Use :meth:`emit` to write the source tree to disk, or
    :meth:`build` to get the in-memory rendering as
    ``{relative_path: source_text}`` for testing.
    """

    schema_path: str
    package: str = "io.example"

    _sv: SchemaView = field(init=False)
    _env: Environment = field(init=False)
    _induced_slots_cache: dict[str, list[SlotDefinition]] = field(
        init=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        self._sv = SchemaView(self.schema_path)
        self._env = Environment(
            loader=FileSystemLoader(_TEMPLATES_DIR),
            autoescape=select_autoescape(disabled_extensions=("jinja",)),
            trim_blocks=False,
            lstrip_blocks=False,
            keep_trailing_newline=True,
        )
        self._chains_index = build_parent_chains_index(
            self._sv,
            resource_classes=self._resource_class_names(),
            excluded_classes=set(),
            is_slot_excluded=lambda s: False,
            get_slot_annotation=self._get_slot_annotation_compat,
            induced_slots=self._induced_slots,
        )

    # --- Public API ---------------------------------------------------

    def emit(self, output_dir: str | Path) -> list[Path]:
        """Render the source tree under ``output_dir/<package_path>/...``.

        Also writes the canonical OpenAPI spec to a sibling
        ``resources/`` directory (Spring Boot's classpath convention),
        so a runtime serdes library can load it via
        ``classpath:openapi.yaml`` and read the ``x-rdf-class`` /
        ``x-rdf-property`` extensions to drive RDF marshaling. The
        spec is the source of truth for class→IRI and slot→predicate
        mappings; no parallel Java annotations needed.

        Returns the list of files written.
        """
        out = Path(output_dir)
        written: list[Path] = []
        for relpath, source in self.build().items():
            target = out / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
            written.append(target)

        # Sidecar OpenAPI spec for the runtime serdes library.
        # `out` is typically `<project>/src/main/java`; the resources
        # dir is `<project>/src/main/resources` per Spring Boot
        # convention. Falling back to `out/resources/` when the
        # parent doesn't look like a Maven/Gradle source tree keeps
        # this useful in toy/test layouts too.
        resources_dir = (
            out.parent / "resources" if out.name == "java" else out / "resources"
        )
        resources_dir.mkdir(parents=True, exist_ok=True)
        spec_path = resources_dir / "openapi.yaml"
        spec_path.write_text(self._render_openapi_spec())
        written.append(spec_path)
        return written

    def _render_openapi_spec(self) -> str:
        """Generate the canonical OpenAPI spec from the same LinkML
        schema. Reuses :class:`OpenAPIGenerator` so the spec carries
        the same ``x-rdf-class`` / ``x-rdf-property`` / discriminator
        / legacy-type-field annotations the in-tree linkml-openapi
        emitter produces."""
        from linkml_openapi.generator import OpenAPIGenerator

        return OpenAPIGenerator(self.schema_path).serialize()

    def build(self) -> dict[str, str]:
        """In-memory rendering. Returns {relative_path: java_source}."""
        files: dict[str, str] = {}
        package_path = self.package.replace(".", "/")

        for class_name in self._sv.all_classes():
            cls = self._sv.get_class(class_name)
            if cls is None:
                continue
            files[f"{package_path}/model/{class_name}.java"] = self._render_dto(cls)
            if self._is_resource(cls):
                files[f"{package_path}/api/{class_name}Api.java"] = self._render_api(cls)
        files[f"{package_path}/model/Problem.java"] = self._render_problem_dto()
        return files

    def _render_problem_dto(self) -> str:
        """Emit an RFC 7807 ``application/problem+json`` DTO. Used as
        the error body schema for non-2xx responses on every operation
        — see :meth:`_top_level_ops` and :meth:`_nested_ops`."""
        template = """package %(package)s.model;

import io.swagger.v3.oas.annotations.media.Schema;

/**
 * RFC 7807 problem details, served as the body of non-2xx responses
 * with content type application/problem+json.
 */
public class Problem {

    @Schema(description = "URI reference identifying the problem type.")
    private String type;

    @Schema(description = "Short, human-readable summary of the problem type.")
    private String title;

    @Schema(description = "HTTP status code.")
    private Integer status;

    @Schema(description = "Human-readable explanation specific to this occurrence.")
    private String detail;

    @Schema(description = "URI reference that identifies the specific occurrence.")
    private String instance;

    public String getType() { return type; }
    public void setType(String type) { this.type = type; }
    public String getTitle() { return title; }
    public void setTitle(String title) { this.title = title; }
    public Integer getStatus() { return status; }
    public void setStatus(Integer status) { this.status = status; }
    public String getDetail() { return detail; }
    public void setDetail(String detail) { this.detail = detail; }
    public String getInstance() { return instance; }
    public void setInstance(String instance) { this.instance = instance; }
}
"""
        return template % {"package": self.package}

    # --- DTO emission -------------------------------------------------

    def _render_dto(self, cls: ClassDefinition) -> str:
        properties, imports = self._collect_properties(cls)
        json_type_info = self._json_type_info(cls)
        if json_type_info is not None:
            imports.update(
                {
                    "com.fasterxml.jackson.annotation.JsonTypeInfo",
                    "com.fasterxml.jackson.annotation.JsonSubTypes",
                }
            )

        # `extends`: parent class name (in same package, no import needed
        # because Java auto-imports same-package types).
        extends = cls.is_a if cls.is_a else None
        if extends is not None and self._sv.get_class(extends) is None:
            extends = None  # external is_a not in this schema

        class_uri = self._expand_curie(cls.class_uri) if cls.class_uri else ""
        # Class-level @Schema(description=…, extensions=…) — the
        # description field carries the LinkML doc + RDF identity
        # footer so Swagger UI's schema panel displays both as plain
        # readable text. The x-rdf-class extension stays separately
        # for spec-driven serdes consumption.
        class_schema_annotation = None
        class_description = _description_with_rdf(cls.description, cls.class_uri)
        if class_uri or class_description:
            imports.add("io.swagger.v3.oas.annotations.media.Schema")
            parts: list[str] = []
            if class_description:
                parts.append(f'description = "{_escape_java(class_description)}"')
            if class_uri:
                imports.add("io.swagger.v3.oas.annotations.extensions.Extension")
                imports.add(
                    "io.swagger.v3.oas.annotations.extensions.ExtensionProperty"
                )
                # Emit both ``x-rdf-class`` (our custom, what the
                # spec-driven serdes runtime currently reads) and
                # ``x-jsonld-type`` (IETF draft-polli-restapi-ld-
                # keywords-02 standard name). Same IRI value;
                # consumers can use either keyword.
                parts.append(
                    "extensions = @Extension(properties = {"
                    '@ExtensionProperty(name = "x-rdf-class", '
                    f'value = "{class_uri}"), '
                    '@ExtensionProperty(name = "x-jsonld-type", '
                    f'value = "{class_uri}")'
                    "})"
                )
            class_schema_annotation = f"@Schema({', '.join(parts)})"

        return self._env.get_template("dto.java.jinja").render(
            package=self.package,
            class_name=cls.name,
            extends=extends,
            abstract=bool(cls.abstract),
            doc=cls.description or "",
            class_uri=class_uri,
            class_schema_annotation=class_schema_annotation,
            json_type_info=json_type_info,
            properties=properties,
            imports=sorted(imports),
        )

    def _collect_properties(
        self, cls: ClassDefinition
    ) -> tuple[list[dict], set[str]]:
        """Build the per-class property list (LOCAL slots only, no
        inherited — those come via Java ``extends``). Plus the synthesised
        discriminator and legacy-type fields when this class is a
        concrete polymorphic leaf."""
        imports: set[str] = set()
        properties: list[dict] = []
        local_slot_names = self._local_slot_names(cls)

        for slot_name in local_slot_names:
            slot = self._slot_for(cls, slot_name)
            if slot is None:
                continue
            prop = self._slot_to_property(slot, imports)
            if prop is not None:
                properties.append(prop)

        # The discriminator field (resourceType) is intentionally not
        # declared on the Java class — Jackson's @JsonTypeInfo +
        # @JsonSubTypes on the polymorphic root injects the value at
        # write time. Declaring a parallel field would cause a
        # duplicate "resourceType":"Catalog" in the JSON output.

        # Legacy back-compat marker (#type / legacyType) — declared as
        # a real Java field with @JsonProperty alias because it carries
        # an opaque class FQN value that Jackson can't synthesise from
        # the type hierarchy alone.
        if not cls.abstract:
            legacy_field = self._inherited_legacy_field(cls)
            if legacy_field is not None:
                legacy_value = self._class_annotation(
                    cls, "openapi.legacy_type_value"
                )
                if legacy_value:
                    legacy_codegen_name = self._class_annotation(
                        self._discriminator_root(cls) or cls,
                        "openapi.legacy_type_codegen_name",
                    )
                    java_name = legacy_codegen_name or _java_identifier(legacy_field)
                    properties.append(
                        {
                            "java_name": java_name,
                            "java_type": "String",
                            "getter_name": java_name[:1].upper() + java_name[1:],
                            "json_property": legacy_field,
                            "required": True,
                            "default": f'"{legacy_value}"',
                            "schema_annotation": (
                                f'@Schema(allowableValues = {{"{legacy_value}"}}, '
                                f'defaultValue = "{legacy_value}")'
                            ),
                            "javadoc": (
                                f'Back-compat opaque type marker. Pinned to '
                                f'"{legacy_value}" for {cls.name}.'
                            ),
                        }
                    )
                    imports.add("com.fasterxml.jackson.annotation.JsonProperty")
                    imports.add("io.swagger.v3.oas.annotations.media.Schema")

        # @NotNull / @JsonProperty imports
        if any(p.get("required") for p in properties):
            imports.add("jakarta.validation.constraints.NotNull")
        if any(p.get("json_property") for p in properties):
            imports.add("com.fasterxml.jackson.annotation.JsonProperty")

        return properties, imports

    def _slot_to_property(
        self, slot: SlotDefinition, imports: set[str]
    ) -> dict | None:
        """Render a LinkML slot as a Java DTO property dict.

        Honours the same composition vs reference logic as the OpenAPI
        emitter:
        * range is identifier-bearing class + ``inlined: false`` →
          ``String`` (URI)
        * range is identifier-less class (or ``inlined: true``) →
          ``$ref``-equivalent (Java type with same package)
        * primitive ranges map via :data:`_RANGE_TYPE_MAP`

        Bean-validation constraints (``@Pattern``, ``@Min``, ``@Max``,
        ``@Size``) flow from LinkML's ``pattern`` / ``minimum_value`` /
        ``maximum_value``. springdoc reads them and surfaces the
        constraints in the OpenAPI spec; Spring's
        ``@Validated`` runs them at request-binding time.
        """
        range_name = slot.range or "string"
        target_cls = self._sv.get_class(range_name)

        if target_cls is not None:
            target_id = self._has_identifier(target_cls)
            if target_id and not slot.inlined:
                # Reference slot (LinkML default for identifier-bearing
                # ranges) → IRI link, typed as ``java.net.URI`` so the
                # Java surface advertises "this is an RDF reference,
                # not an opaque string".
                java_inner = "java.net.URI"
                imports.add("java.net.URI")
                json_prop = None
            else:
                java_inner = target_cls.name
                json_prop = None
        else:
            mapping = _RANGE_TYPE_MAP.get(range_name, ("String", None))
            java_inner, importable = mapping
            if importable:
                imports.add(importable)
            json_prop = None

        if slot.multivalued:
            java_type = f"java.util.List<{java_inner}>"
            imports.add("java.util.List")
        else:
            java_type = java_inner

        validation_annotations: list[str] = []
        if slot.pattern:
            escaped = slot.pattern.replace("\\", "\\\\").replace('"', '\\"')
            validation_annotations.append(f'@Pattern(regexp = "{escaped}")')
            imports.add("jakarta.validation.constraints.Pattern")
        if slot.minimum_value is not None and java_inner in {"Long", "Integer"}:
            validation_annotations.append(f"@Min({slot.minimum_value})")
            imports.add("jakarta.validation.constraints.Min")
        if slot.maximum_value is not None and java_inner in {"Long", "Integer"}:
            validation_annotations.append(f"@Max({slot.maximum_value})")
            imports.add("jakarta.validation.constraints.Max")

        # Field-level @Schema with description + x-rdf-property — the
        # description merges the LinkML slot doc with an "RDF
        # property: `<curie>`" footer so Swagger UI's property panel
        # shows it as the primary docs text. The extension is kept
        # for spec-driven serdes layers.
        rdf_property: str | None = None
        if slot.slot_uri:
            try:
                rdf_property = self._sv.expand_curie(slot.slot_uri)
            except Exception:
                rdf_property = slot.slot_uri
        slot_description = _description_with_rdf(
            slot.description, slot.slot_uri, kind="property"
        )
        schema_annotation = None
        if rdf_property or slot_description:
            imports.add("io.swagger.v3.oas.annotations.media.Schema")
            parts: list[str] = []
            if slot_description:
                parts.append(f'description = "{_escape_java(slot_description)}"')
            if rdf_property:
                imports.add("io.swagger.v3.oas.annotations.extensions.Extension")
                imports.add(
                    "io.swagger.v3.oas.annotations.extensions.ExtensionProperty"
                )
                parts.append(
                    'extensions = @Extension(properties = '
                    '@ExtensionProperty(name = "x-rdf-property", '
                    f'value = "{rdf_property}"))'
                )
            schema_annotation = f"@Schema({', '.join(parts)})"

        java_name = _java_identifier(slot.name)
        return {
            "java_name": java_name,
            "java_type": java_type,
            "getter_name": java_name[:1].upper() + java_name[1:],
            "json_property": (
                slot.name if slot.name != java_name else json_prop
            ),
            "required": bool(slot.required),
            "default": None,
            "javadoc": (slot.description or "").strip() or None,
            "validation_annotations": validation_annotations,
            "schema_annotation": schema_annotation,
        }

    # --- API emission -------------------------------------------------

    def _render_api(self, cls: ClassDefinition) -> str:
        path_segment = self._path_segment(cls)
        media_types = self._media_types(cls)
        ops: list[dict] = []
        imports: set[str] = {
            "java.util.List",
            "org.springframework.http.HttpStatus",
            "org.springframework.http.ResponseEntity",
            "org.springframework.web.bind.annotation.DeleteMapping",
            "org.springframework.web.bind.annotation.GetMapping",
            "org.springframework.web.bind.annotation.PathVariable",
            "org.springframework.web.bind.annotation.PostMapping",
            "org.springframework.web.bind.annotation.PutMapping",
            "org.springframework.web.bind.annotation.RequestBody",
            "org.springframework.web.bind.annotation.RequestParam",
            "io.swagger.v3.oas.annotations.responses.ApiResponse",
            "io.swagger.v3.oas.annotations.media.ArraySchema",
            "io.swagger.v3.oas.annotations.media.Content",
            "io.swagger.v3.oas.annotations.media.Schema",
            f"{self.package}.model.{cls.name}",
            f"{self.package}.model.Problem",
        }
        # Mutex check: nested_only and flat_only contradict.
        nested_only_raw = self._class_annotation(cls, "openapi.nested_only")
        flat_only_raw = self._class_annotation(cls, "openapi.flat_only")
        nested_only = (
            nested_only_raw is not None
            and nested_only_raw.strip().lower() == "true"
        )
        flat_only = (
            flat_only_raw is not None
            and flat_only_raw.strip().lower() == "true"
        )
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
        # Deep nested chain (auto-derived). Item-only CRUD on the deep URL.
        if not flat_only:
            parent_path_ann = self._class_annotation(cls, "openapi.parent_path")
            chain = canonical_parent_chain(cls.name, self._chains_index, parent_path_ann)
            if chain:
                ops.extend(self._deep_chained_ops(cls, chain, imports, media_types))
        # Decorate every op with explicit success + RFC 7807 error
        # responses. The success block fans out one ``@Content`` per
        # advertised media type so the live spec advertises
        # ``application/ld+json`` / ``text/turtle`` etc. under the
        # 200 response (not only the default JSON). springdoc drops
        # auto-detected 200 when any ``@ApiResponse`` is present.
        for op in ops:
            op.setdefault("method_annotations", []).extend(
                _success_and_problem_responses(op["return_type"], media_types)
            )
        return self._env.get_template("api.java.jinja").render(
            package=self.package,
            resource_class=cls.name,
            class_uri=self._expand_curie(cls.class_uri) if cls.class_uri else "",
            imports=sorted(imports),
            operations=ops,
        )

    def _top_level_ops(
        self,
        cls: ClassDefinition,
        path_segment: str,
        imports: set[str],
        media_types: list[str],
    ) -> list[dict]:
        """Standard CRUD on the resource itself."""
        cn = cls.name
        base = f'"/{path_segment}"'
        item = f'"/{path_segment}/{{id}}"'
        list_return = f"List<{cn}>"
        produces = _produces_arg(media_types)
        consumes = produces
        return [
            {
                "javadoc": f"GET /{path_segment} — list {cn}s. Default returns 501.",
                "method_annotations": [f"@GetMapping(value = {base}, produces = {produces})"],
                "method_name": f"list{cn}s",
                "return_type": list_return,
                "params": _list_query_params() + self._query_param_dicts(cls, imports),
            },
            {
                "javadoc": f"POST /{path_segment} — create a {cn}.",
                "method_annotations": [
                    f"@PostMapping(value = {base}, consumes = {consumes}, produces = {produces})"
                ],
                "method_name": f"create{cn}",
                "return_type": cn,
                "params": [
                    {"annotation": "@RequestBody", "java_type": cn, "java_name": "body"},
                ],
            },
            {
                "javadoc": f"GET /{path_segment}/{{id}} — read a {cn}.",
                "method_annotations": [f"@GetMapping(value = {item}, produces = {produces})"],
                "method_name": f"get{cn}",
                "return_type": cn,
                "params": [
                    {
                        "annotation": '@PathVariable("id")',
                        "java_type": "String",
                        "java_name": "id",
                    }
                ],
            },
            {
                "javadoc": f"PUT /{path_segment}/{{id}} — replace a {cn}.",
                "method_annotations": [
                    f"@PutMapping(value = {item}, consumes = {consumes}, produces = {produces})"
                ],
                "method_name": f"update{cn}",
                "return_type": cn,
                "params": [
                    {
                        "annotation": '@PathVariable("id")',
                        "java_type": "String",
                        "java_name": "id",
                    },
                    {"annotation": "@RequestBody", "java_type": cn, "java_name": "body"},
                ],
            },
            {
                "javadoc": f"DELETE /{path_segment}/{{id}} — delete a {cn}.",
                "method_annotations": [f"@DeleteMapping({item})"],
                "method_name": f"delete{cn}",
                "return_type": "Void",
                "params": [
                    {
                        "annotation": '@PathVariable("id")',
                        "java_type": "String",
                        "java_name": "id",
                    }
                ],
            },
        ]


    def _nested_ops(
        self,
        cls: ClassDefinition,
        path_segment: str,
        imports: set[str],
        media_types: list[str],
    ) -> list[dict]:
        """For each class slot (inherited or local) that ranges on
        another class, emit either nested CRUD (composition) or
        attach/detach (reference). Inherited slots count too — a
        Catalog inherits all of Resource's agent / version /
        relation slots and we want the API surface to expose them
        consistently."""
        ops: list[dict] = []
        for slot in self._sv.class_induced_slots(cls.name):
            target = self._sv.get_class(slot.range or "")
            if target is None:
                continue
            if not self._has_identifier(target):
                # Embedded value class (no id) — composition is implicit
                # via the embedded type; no extra endpoints needed.
                continue
            target_id_path = "{" + _java_identifier(target.name) + "Id}"
            slot_seg = self._render_slot_segment_compat(cls, slot)
            collection = f'"/{path_segment}/{{id}}/{slot_seg}"'
            item = f'"/{path_segment}/{{id}}/{slot_seg}/{target_id_path}"'

            if slot.inlined:
                ops.extend(self._composition_ops(
                    cls, target, slot, collection, item, imports, media_types))
            else:
                ops.extend(self._reference_ops(
                    cls, target, slot, collection, item, imports, media_types))
        return ops

    def _composition_ops(
        self,
        parent: ClassDefinition,
        target: ClassDefinition,
        slot: SlotDefinition,
        collection: str,
        item: str,
        imports: set[str],
        media_types: list[str],
    ) -> list[dict]:
        """Full CRUD nested under the parent — bodies and responses
        are typed as the target class because composition embeds it."""
        imports.add(f"{self.package}.model.{target.name}")
        pn, tn, sn = parent.name, target.name, _camel(slot.name)
        target_id_var = _java_identifier(target.name) + "Id"
        produces = _produces_arg(media_types)
        consumes = produces
        return [
            {
                "javadoc": f"GET /{slot.name} — list embedded {tn}s under a {pn}.",
                "method_annotations": [f"@GetMapping(value = {collection}, produces = {produces})"],
                "method_name": f"list{pn}{sn}",
                "return_type": f"List<{tn}>",
                "params": [
                    _path_param("id"),
                    *_list_query_params(),
                    *self._query_param_dicts(target, imports),
                ],
            },
            {
                "javadoc": f"POST /{slot.name} — create a {tn} embedded under a {pn}.",
                "method_annotations": [
                    f"@PostMapping(value = {collection}, consumes = {consumes}, produces = {produces})"
                ],
                "method_name": f"create{pn}{sn}",
                "return_type": tn,
                "params": [
                    _path_param("id"),
                    {"annotation": "@RequestBody", "java_type": tn, "java_name": "body"},
                ],
            },
            {
                "javadoc": f"GET /{slot.name}/{{id}} — read embedded {tn}.",
                "method_annotations": [f"@GetMapping(value = {item}, produces = {produces})"],
                "method_name": f"get{pn}{sn}Item",
                "return_type": tn,
                "params": [
                    _path_param("id"),
                    _path_param(target_id_var),
                ],
            },
            {
                "javadoc": f"PUT /{slot.name}/{{id}} — replace embedded {tn}.",
                "method_annotations": [
                    f"@PutMapping(value = {item}, consumes = {consumes}, produces = {produces})"
                ],
                "method_name": f"update{pn}{sn}Item",
                "return_type": tn,
                "params": [
                    _path_param("id"),
                    _path_param(target_id_var),
                    {"annotation": "@RequestBody", "java_type": tn, "java_name": "body"},
                ],
            },
            {
                "javadoc": f"DELETE /{slot.name}/{{id}} — remove embedded {tn}.",
                "method_annotations": [f"@DeleteMapping({item})"],
                "method_name": f"delete{pn}{sn}Item",
                "return_type": "Void",
                "params": [
                    _path_param("id"),
                    _path_param(target_id_var),
                ],
            },
        ]

    def _reference_ops(
        self,
        parent: ClassDefinition,
        target: ClassDefinition,
        slot: SlotDefinition,
        collection: str,
        item: str,
        imports: set[str],
        media_types: list[str],
    ) -> list[dict]:
        """Attach (POST IRI) and detach (DELETE) — the relationship
        target is identified by IRI; lifecycle is owned elsewhere.
        Bodies and list returns are typed as ``java.net.URI`` for
        RDF-link consistency with the DTO field types."""
        imports.add("java.net.URI")
        pn, tn, sn = parent.name, target.name, _camel(slot.name)
        target_id_var = _java_identifier(target.name) + "Id"
        produces = _produces_arg(media_types)
        consumes = produces
        return [
            {
                "javadoc": (
                    f"GET /{slot.name} — list IRIs of attached {tn}s "
                    f"on this {pn}."
                ),
                "method_annotations": [f"@GetMapping(value = {collection}, produces = {produces})"],
                "method_name": f"list{pn}{sn}Refs",
                "return_type": "List<URI>",
                "params": [
                    _path_param("id"),
                    *_list_query_params(),
                    *self._query_param_dicts(target, imports),
                ],
            },
            {
                "javadoc": (
                    f"POST /{slot.name} — attach an existing {tn} "
                    f"by IRI to this {pn}."
                ),
                "method_annotations": [
                    f"@PostMapping(value = {collection}, consumes = {consumes})"
                ],
                "method_name": f"attach{pn}{sn}",
                "return_type": "Void",
                "params": [
                    _path_param("id"),
                    {
                        "annotation": "@RequestBody",
                        "java_type": "URI",
                        "java_name": "targetIri",
                    },
                ],
            },
            {
                "javadoc": (
                    f"DELETE /{slot.name}/{{id}} — detach a {tn} from "
                    f"this {pn}."
                ),
                "method_annotations": [f"@DeleteMapping({item})"],
                "method_name": f"detach{pn}{sn}",
                "return_type": "Void",
                "params": [
                    _path_param("id"),
                    _path_param(target_id_var),
                ],
            },
        ]

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
        #   - parent_id_param_name: snake_case path_id for this parent (or
        #     openapi.path_id override)
        #   - slot_segment: the slot from this parent leading to the next hop
        #
        # Leaf id stays {id} to match Spring's existing flat-URL convention
        # in _top_level_ops (which uses /{path_segment}/{{id}}). Ancestor
        # segments DO honor openapi.path_id.
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

    # --- Query-param surface -------------------------------------------

    def _query_param_dicts(
        self, cls: ClassDefinition, imports: set[str]
    ) -> list[dict]:
        """Slot-driven @RequestParam dicts for a list endpoint on `cls`.

        Returns dicts shaped like _list_query_params() so the
        api.java.jinja template renders them without changes. Limit/offset
        are NOT included here — those still come from _list_query_params.
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
        return raw.strip().lower() == "true"

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
        to String for query-param purposes."""
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

    # --- LinkML helpers ------------------------------------------------

    def _is_resource(self, cls: ClassDefinition) -> bool:
        return self._class_annotation(cls, "openapi.resource") == "true"

    def _media_types(self, cls: ClassDefinition) -> list[str]:
        """Per-class media type list. Walks the ``is_a`` chain, taking
        the first ``openapi.media_types`` annotation found (so a
        polymorphic root like ``Resource`` advertises the same RDF
        formats across every concrete subclass without each one having
        to repeat the annotation). Defaults to JSON only.

        Used to populate ``produces`` / ``consumes`` on Spring method
        annotations so springdoc advertises every supported wire format
        and clients can content-negotiate (``Accept: text/turtle``,
        ``Accept: application/ld+json`` etc.). The actual marshaling
        for non-JSON types lives in a separate runtime library — the
        controller methods just declare the contract.
        """
        cur: ClassDefinition | None = cls
        while cur is not None:
            raw = self._class_annotation(cur, "openapi.media_types")
            if raw:
                return [m.strip() for m in raw.split(",") if m.strip()]
            cur = self._sv.get_class(cur.is_a) if cur.is_a else None
        return ["application/json"]

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

    def _path_segment(self, cls: ClassDefinition) -> str:
        """URL path segment for a class.

        Priority:
        1. ``openapi.path`` annotation — taken verbatim, no path-style transform.
        2. Auto-derived ``<class_snake>s`` with active path-style applied.
        """
        explicit = self._class_annotation(cls, "openapi.path")
        if explicit:
            return explicit.strip()
        return self._apply_path_style(_to_snake_case(cls.name) + "s")

    def _render_slot_segment_compat(
        self, parent_cls: ClassDefinition | None, slot: SlotDefinition
    ) -> str:
        """Slot segment for chain / nested URLs.

        Priority:
        1. ``openapi.path_segment`` slot annotation — verbatim.
        2. Slot name with active path-style applied.
        """
        if parent_cls is not None:
            explicit = self._get_slot_annotation_compat(
                parent_cls, slot.name, "openapi.path_segment"
            )
            if explicit:
                return explicit.strip()
        return self._apply_path_style(slot.name)

    def _induced_slots(
        self, class_name: str
    ) -> list[SlotDefinition]:
        """Cached wrapper around ``SchemaView.class_induced_slots``.

        ``class_induced_slots`` is an expensive walk (merges ``is_a``
        chain, applies ``slot_usage``) and we hit it many times per
        class per render — once for the local-slot diff, once for
        identifier detection, once per slot lookup, once per nested-op
        target check. A simple per-instance cache keyed by class name
        eliminates the redundant traversals for the duration of one
        ``build()`` call without changing any observable behaviour.
        """
        cached = self._induced_slots_cache.get(class_name)
        if cached is None:
            cached = list(self._sv.class_induced_slots(class_name))
            self._induced_slots_cache[class_name] = cached
        return cached

    def _has_identifier(self, cls: ClassDefinition) -> bool:
        return any(slot.identifier for slot in self._induced_slots(cls.name))

    def _local_slot_names(self, cls: ClassDefinition) -> list[str]:
        own = self._induced_slots(cls.name)
        if not cls.is_a:
            return [s.name for s in own]
        parent_slots = {s.name for s in self._induced_slots(cls.is_a)}
        return [s.name for s in own if s.name not in parent_slots]

    def _slot_for(self, cls: ClassDefinition, slot_name: str) -> SlotDefinition | None:
        for slot in self._induced_slots(cls.name):
            if slot.name == slot_name:
                return slot
        return None

    def _class_annotation(self, cls: ClassDefinition, tag: str) -> str | None:
        if not cls or not cls.annotations:
            return None
        for ann in cls.annotations.values():
            if ann.tag == tag:
                return str(ann.value)
        return None

    def _resource_class_names(self) -> set[str]:
        out: set[str] = set()
        for name in self._sv.all_classes():
            cls = self._sv.get_class(name)
            if cls and self._is_resource(cls):
                out.add(name)
        return out

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

    def _identifier_slot_for(self, class_name: str) -> SlotDefinition | None:
        for s in self._induced_slots(class_name):
            if s.identifier:
                return s
        return None

    def _induced_slots_by_name(self, class_name: str) -> dict[str, SlotDefinition]:
        return {s.name: s for s in self._induced_slots(class_name)}

    def _expand_curie(self, curie: str) -> str:
        try:
            return self._sv.expand_curie(curie)
        except Exception:
            return curie

    # --- Polymorphism --------------------------------------------------

    def _discriminator_field(self, cls: ClassDefinition) -> str | None:
        return self._class_annotation(cls, "openapi.discriminator")

    def _inherited_discriminator(self, cls: ClassDefinition) -> str | None:
        cur: ClassDefinition | None = cls
        while cur is not None:
            field_name = self._discriminator_field(cur)
            if field_name:
                return field_name.strip()
            cur = self._sv.get_class(cur.is_a) if cur.is_a else None
        return None

    def _discriminator_root(self, cls: ClassDefinition) -> ClassDefinition | None:
        cur: ClassDefinition | None = cls
        last: ClassDefinition | None = None
        while cur is not None:
            if self._discriminator_field(cur):
                last = cur
            cur = self._sv.get_class(cur.is_a) if cur.is_a else None
        return last

    def _inherited_legacy_field(self, cls: ClassDefinition) -> str | None:
        root = self._discriminator_root(cls)
        if root is None:
            return None
        return self._class_annotation(root, "openapi.legacy_type_field")

    def _type_value(self, cls: ClassDefinition) -> str:
        override = self._class_annotation(cls, "openapi.type_value")
        return override.strip() if override else cls.name

    def _json_type_info(self, cls: ClassDefinition) -> dict | None:
        """Return the @JsonTypeInfo + @JsonSubTypes data when ``cls`` is
        the *root* of a polymorphic chain (declares the discriminator
        and has at least one concrete subclass)."""
        field_name = self._discriminator_field(cls)
        if field_name is None:
            return None
        # Only the root carries the annotation. If a parent in the
        # chain also declares the discriminator, defer to it.
        parent = self._sv.get_class(cls.is_a) if cls.is_a else None
        if parent is not None and self._inherited_discriminator(parent):
            return None
        subtypes = []
        for name in self._sv.class_descendants(cls.name, reflexive=False):
            sub = self._sv.get_class(name)
            if sub is None or sub.abstract or sub.mixin:
                continue
            subtypes.append(
                {"class_name": sub.name, "tag": self._type_value(sub)}
            )
        if not subtypes:
            return None
        return {"property": field_name.strip(), "subtypes": subtypes}


# --- module-level helpers ----------------------------------------------




def _produces_arg(media_types: list[str]) -> str:
    """Java array literal for ``produces = {…}`` / ``consumes = {…}``
    on a Spring mapping annotation, given the class's negotiated
    media types."""
    return "{" + ", ".join(f'"{m}"' for m in media_types) + "}"


def _description_with_rdf(
    description: str | None, rdf_curie: str | None, kind: str = "class"
) -> str | None:
    """Compose a single description string from a LinkML doc and an
    RDF identity CURIE. Footer wording matches
    :meth:`OpenAPIGenerator._class_description` /
    :meth:`OpenAPIGenerator._slot_description` so the Spring DTO
    documentation reads identically to the OpenAPI spec's description
    fields — ``RDF class: `<curie>``` / ``RDF property: `<curie>```.

    The CURIE form is chosen over the expanded IRI: shorter,
    recognisable to anyone who has seen the vocabulary, and the
    full IRI is in the ``x-rdf-*`` extension next to it.
    """
    parts: list[str] = []
    if description:
        parts.append(description.strip())
    if rdf_curie:
        parts.append(f"RDF {kind}: `{rdf_curie}`")
    return "\n\n".join(parts) or None


def _escape_java(text: str) -> str:
    """Escape a string for embedding inside a Java string literal:
    backslashes, double quotes, newlines, tabs, carriage returns.
    Anything else passes through untouched."""
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _java_identifier(s: str) -> str:
    """Sanitise ``s`` to a valid Java field identifier — keep
    alphanumerics, drop everything else (``#``, ``@``, ``-``, etc.).
    Empty / illegal-leading-char results fall back to ``field``."""
    out = re.sub(r"[^A-Za-z0-9_]", "", s).lstrip("0123456789")
    return out or "field"


def _camel(s: str) -> str:
    """Convert ``slot_name`` / ``slot-name`` to ``SlotName`` for
    constructing Java method names from LinkML slot names."""
    parts = re.split(r"[_\-\s]+", s)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _path_param(name: str) -> dict:
    return {
        "annotation": f'@PathVariable("{name}")',
        "java_type": "String",
        "java_name": name,
    }


def _list_query_params() -> list[dict]:
    """``limit`` / ``offset`` paging on every list/collection endpoint.
    Defaults are conservative — 50 / 0. Override via the request URL."""
    return [
        {
            "annotation": '@RequestParam(name = "limit", required = false, defaultValue = "50")',
            "java_type": "Integer",
            "java_name": "limit",
        },
        {
            "annotation": '@RequestParam(name = "offset", required = false, defaultValue = "0")',
            "java_type": "Integer",
            "java_name": "offset",
        },
    ]


def _success_and_problem_responses(
    return_type: str, media_types: list[str]
) -> list[str]:
    """Success + RFC 7807 error responses for an operation.

    Springdoc drops the auto-detected ``200`` whenever any
    ``@ApiResponse`` annotation is present, so the success entry has
    to be declared explicitly alongside the error contract. The
    success response fans out one ``@Content`` block per advertised
    media type so the live spec advertises every negotiated format
    under ``responses.200.content``.
    """
    if return_type == "Void":
        return [
            '@ApiResponse(responseCode = "204", description = "No content")',
            *_problem_responses(),
        ]
    if return_type.startswith("List<"):
        inner = return_type[len("List<") : -1]
        contents = [
            (
                f'@Content(mediaType = "{mt}",'
                f' array = @ArraySchema(schema = @Schema(implementation = {inner}.class)))'
            )
            for mt in media_types
        ]
    else:
        contents = [
            (
                f'@Content(mediaType = "{mt}",'
                f" schema = @Schema(implementation = {return_type}.class))"
            )
            for mt in media_types
        ]
    success = (
        '@ApiResponse(responseCode = "200", description = "OK",'
        f" content = {{{', '.join(contents)}}})"
    )
    return [success, *_problem_responses()]


def _problem_responses() -> list[str]:
    """RFC 7807 error contract — same Problem shape under
    ``application/problem+json`` for 404/422/500 across every
    operation."""
    return [
        '@ApiResponse(responseCode = "404", description = "Not found",'
        ' content = @Content(mediaType = "application/problem+json",'
        ' schema = @Schema(implementation = Problem.class)))',
        '@ApiResponse(responseCode = "422", description = "Validation error",'
        ' content = @Content(mediaType = "application/problem+json",'
        ' schema = @Schema(implementation = Problem.class)))',
        '@ApiResponse(responseCode = "500", description = "Server error",'
        ' content = @Content(mediaType = "application/problem+json",'
        ' schema = @Schema(implementation = Problem.class)))',
    ]
