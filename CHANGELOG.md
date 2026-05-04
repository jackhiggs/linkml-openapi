# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(while pre-1.0, minor bumps may carry visible behaviour changes).

## [0.10.0] — 2026-05-04

### Added

- **URL path prefix on both emitters** — `--path-prefix /PREFIX`
  (CLI) / `path_prefix=` (Python) / schema-level
  `openapi.path_prefix` annotation. Resolution order: kwarg → schema
  annotation → no prefix. The prefix must start with `/`, must not
  contain `{…}` placeholders, and trailing `/` is normalised.
  ([#61](https://github.com/jackhiggs/linkml-openapi/issues/61))
  - **OpenAPI generator** prepends the prefix to every key under
    `paths:`. Validates that no class-level `openapi.path` /
    `openapi.path_template` already starts with the prefix
    (catches doubled-prefix configs at build time). `servers[0].url`
    is left untouched — pass `--server-url` separately if you want
    the prefix in the server URL too.
  - **Spring server emitter** renders a class-level
    `@RequestMapping("<prefix>")` on every controller interface
    (Spring idiom — method-level mappings stay relative). The
    sidecar `resources/openapi.yaml` adopts the same prefix on its
    `paths:` keys so springdoc's runtime view matches the static
    spec.

## [0.9.0] — 2026-05-04

The ship-it release for the **LinkML → Spring server emitter** —
generates Spring Boot DTOs and `@RestController` interfaces directly
from a LinkML schema, with a runtime sidecar OpenAPI spec carrying
every `x-rdf-class` / `x-rdf-property` extension. Pairs with the new
[`spring-rdf-runtime`](examples/spring-rdf-runtime/) library and the
end-to-end DCAT-3 demo at [`examples/dcat3-demo/`](examples/dcat3-demo/)
to prove the LinkML → OpenAPI → Spring → JSON+RDF round-trip on a
real W3C standard.

### Added — Spring server emitter

- **`gen-spring-server` CLI** + `linkml_openapi.spring.SpringServerGenerator`
  Python API. Walks the LinkML schema, writes
  `<package>/model/*.java` (DTOs with Jackson polymorphism, Bean
  Validation, `@Schema` extensions for RDF metadata) and
  `<package>/api/*Api.java` (controller interfaces with default 501
  bodies). Sidecar OpenAPI spec lands at `resources/openapi.yaml` for
  the runtime serdes layer to read.
- **Polymorphism via `is_a` chains.** LinkML inheritance becomes Java
  `extends`; the polymorphic root carries `@JsonTypeInfo` +
  `@JsonSubTypes`; concrete subclasses pin their discriminator value.
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))
- **Slot-driven query parameters** on every list endpoint, with the
  same capability tokens as the OpenAPI side: `equality`,
  `comparable` (emits `__gte`/`__lte`/`__gt`/`__lt`), `sortable`
  (single `?sort=` array). Auto-inference from scalar slots; opt-out
  via `openapi.auto_query_params: "false"` (schema/class) or
  `openapi.query_param: "false"` (slot).
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))
- **Deep-chained URLs** for leaf classes reachable through a chain of
  multivalued slots: `/catalogs/{cat}/dataset/{ds}/distribution/{id}`.
  Honours `openapi.path_id`, `openapi.parent_path`,
  `openapi.nested_only` / `openapi.flat_only`.
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))
- **Templated paths** via `openapi.path_template` +
  `openapi.path_param_sources` for URL shapes the relationship graph
  can't express (literal segments, compound keys, version prefixes).
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))
- **Path-style + per-slot path-segment overrides.** Schema-level
  `openapi.path_style: kebab-case` renders auto-derived URL nouns
  (`/data-services`, `/contact-point`); slot-level
  `openapi.path_segment` is verbatim.
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))
- **Opt-in camelCase → kebab splitting.** Schema-level
  `openapi.path_split_camel: "true"` (paired with `kebab-case`)
  splits camelCase boundaries (`inSeries` → `in-series`,
  `XMLParser` → `xml-parser`, acronym-aware).
  ([#57](https://github.com/jackhiggs/linkml-openapi/pull/57))
- **Body validation.** `@Valid` on every `@RequestBody` parameter
  triggers Spring's bean-validation pass at request-binding time;
  `@NotNull` / `@Pattern` / `@Min` / `@Max` constraints (already
  emitted on DTOs from LinkML) now actually run.
  ([#59](https://github.com/jackhiggs/linkml-openapi/pull/59))

### Added — Post-processors framework

- **`linkml_openapi.post_processors`** registry + the first concrete
  pass, **`extract-inline-oneof`**, which hoists every inline
  polymorphic `oneOf` in path operations to a named component schema
  so openapi-generator's Spring template preserves discriminator
  dispatch instead of flattening the union.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55))
- **`OpenAPIGenerator.post_processors: list[str]`** field threaded
  through `serialize()` after the canonical spec is built but before
  serialisation. New `--post-process NAME` CLI flag.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55))

### Added — Codegen name-mappings

- **`name_mappings()` / `emit_name_mappings()`** on the generator,
  populated during the build.
- **`openapi.legacy_type_codegen_name`** annotation on a polymorphic
  root: when paired with `openapi.legacy_type_field`, supplies a
  clean target-language identifier (e.g. `legacyType`) for
  openapi-generator's `--name-mappings @file` mechanism. Avoids the
  auto-mangled `hashType` / `atType` names that Java/Spring/TS
  templates produce for awkward wire names like `#type`.
- **`--emit-name-mappings PATH`** CLI flag writes the rename map to
  disk for direct consumption by `openapi-generator-cli`.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55))

### Added — Examples

- **`examples/dcat3-demo/`** — Spring Boot service that runs
  `gen-spring-server` against `tests/fixtures/dcat3.yaml` during
  Maven's `generate-sources` phase, then boots Spring Boot with
  springdoc serving `/v3/api-docs` and `/swagger-ui/index.html`.
  Hand-written `StubControllers.java` delegates to in-memory stores
  with seed data so every endpoint actually responds. Includes
  [README.md](examples/dcat3-demo/README.md) with the run command
  and curl one-liners for the four content-negotiation surfaces.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55),
  [#58](https://github.com/jackhiggs/linkml-openapi/pull/58))
- **`examples/spring-rdf-runtime/`** — Spring Boot library that
  registers an `HttpMessageConverter` for the RDF media types
  (`text/turtle`, `application/ld+json`, `application/rdf+xml`,
  `application/n-triples`). Reads `classpath:openapi.yaml`, indexes
  `x-rdf-class` / `x-rdf-property` extensions at startup, and
  marshals DTOs to RDF graphs via Apache Jena. Drop-in via Spring
  Boot's META-INF AutoConfiguration. dcat3-demo consumes it to show
  end-to-end LinkML → OpenAPI → Spring → JSON / Turtle / JSON-LD /
  RDF/XML round-tripping with no per-class marshaling code.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55))

### Added — Architectural

- **Use-site polymorphic dispatch.** `oneOf` moves from parent
  component schemas to *use sites* — path responses
  (`_class_response_ref`) and slot ranges (`_class_range_ref`) — with
  the union *including* the root itself when concrete. Avoids
  openapi-generator's Spring template choking on a parent that
  self-references in its own `oneOf` (cyclic inheritance).
  Companion methods `_concrete_descendants_excluding_self` (for the
  discriminator-mapping case) and `_concrete_descendants_including_self`
  (for use-site dispatch) make the two requirements explicit.
  ([#55](https://github.com/jackhiggs/linkml-openapi/pull/55))
- **Shared helpers** for the two emitters:
  - `linkml_openapi/_query_params.py` — `walk_query_params()`,
    capability tokens, frozen dataclasses.
  - `linkml_openapi/_chains.py` — `build_parent_chains_index()`,
    `canonical_parent_chain()`, `parse_path_param_sources()`,
    `render_chain_hops()`.

  Both generators consume the same helpers; validation messages stay
  in sync; `tests/test_linkml_spring.py::TestParityWithOpenApiSide`
  catches drift.
  ([#54](https://github.com/jackhiggs/linkml-openapi/pull/54))

### Fixed

- **Spring `_nested_ops` now honors `openapi.nested: "false"`** and
  skips single-valued class-ranged slots (matching the OpenAPI
  generator's behaviour). The bug surfaced live when the dcat3-demo's
  springdoc-generated `/v3/api-docs` advertised 137 paths against a
  62-path sidecar — single-valued slots like `hasCurrentVersion`,
  `replaces` etc. were emitting nested URLs they shouldn't have.
  ([#58](https://github.com/jackhiggs/linkml-openapi/pull/58))

### Distribution

- New runtime dependency: `jinja2>=3.1` (Spring template renderer).
- New project script: `gen-spring-server`.
- New `linkml.generators` entry point: `spring`.

## [0.8.2] — 2026-04-29

### Fixed

- **Discriminator parent excluded from its own `oneOf` and
  `discriminator.mapping`**
  ([#52](https://github.com/jackhiggs/linkml-openapi/issues/52)). The
  0.8.0 emission included the discriminator-root class itself in its
  own `oneOf` array (and `discriminator.mapping`) whenever the root
  wasn't marked ``abstract: true`` in LinkML. openapi-generator's
  Spring server library reads that self-reference as cyclic
  inheritance and fails the build. The discriminator parent is the
  union *category*, never an *instance* of the union; the `oneOf`
  array now contains only descendant `$ref`s, regardless of whether
  the parent is `abstract: true` (LinkML's annotation) or just
  conceptually-but-not-flagged-abstract (DCAT3-style schemas).
  Behaviour for genuinely abstract parents (Animal / Product in the
  fixture) is unchanged — they were already excluded by the existing
  `cls.abstract` filter.

## [0.8.1] — 2026-04-29

### Fixed

- **`type: object` is preserved on discriminator parents under
  `--flatten-inheritance`**
  ([#50](https://github.com/jackhiggs/linkml-openapi/issues/50)). The
  flatten branch added in 0.8.0 stripped ``type``, ``properties``,
  ``required``, and ``additionalProperties`` on discriminator parents
  on the rationale that subclasses inline the parent's slots under
  flatten. That rationale was wrong — the parent is still a concrete
  schema in its own right, and `openapi-generator`'s Spring server
  library (and other codegens) skip generating a class when
  ``type: object`` is missing, breaking the controller compile.
  Polymorphism is orthogonal to whether the parent is a concrete
  schema; ``type`` / ``properties`` / ``required`` are now preserved
  alongside ``oneOf`` / ``discriminator`` regardless of the flatten
  flag. The flag still affects subclass shapes (no ``allOf``
  back-reference) — its actual job — but doesn't strip the parent's
  own emission.

## [0.8.0] — 2026-04-29

Discriminator-completeness release. Polymorphic parent schemas now
emit a `oneOf` array of subclass `$ref`s alongside the existing
`discriminator` block — what Swagger UI and codegen tools (TS / Java /
Spring) actually need to offer polymorphic selection at the call
site. Schemas without a discriminator regenerate byte-identically.

### Added

- **`oneOf` array on discriminator parent schemas**
  ([#47](https://github.com/jackhiggs/linkml-openapi/issues/47)). When
  a class declares ``openapi.discriminator`` (or
  ``designates_type: true``) and concrete subclasses carry
  ``openapi.type_value``, the parent's component schema now carries
  a ``oneOf`` array of ``$ref``s to every concrete subclass —
  alongside the existing ``discriminator`` block. This is what
  Swagger UI and openapi-generator's TypeScript / Java / Spring
  outputs need to offer **polymorphic selection** at the call site;
  the discriminator alone tells consumers how to *interpret* a
  payload but not which subclasses are *possible*.
  - Default: parent keeps its own ``properties`` (so subclasses can
    continue to ``allOf: [parent_ref, local]``) and gains the
    ``oneOf`` alongside.
  - With ``--flatten-inheritance``: parent becomes ``oneOf``-only —
    no ``type``, ``properties``, ``required``, or
    ``additionalProperties``. Subclasses are already self-contained
    under flatten, so the parent doesn't need to carry shape itself.

### Fixed

- The OpenAPI 3.1 structural validator
  (``openapi-spec-validator`` ≤ 0.8.x) can't traverse cyclic
  discriminator schemas — parent ``oneOf`` of subclasses + subclass
  ``allOf`` of parent is intrinsically cyclic, and the underlying
  ``pathable`` walker recurses without cycle detection. Real
  consumers (Swagger UI, openapi-generator, openapi-typescript)
  handle this via ``$ref`` resolution; the e2e validator test now
  documents the limitation and skips for fixtures that exercise the
  pattern.

## [0.7.0] — 2026-04-28

DCAT3 1:1-match release. Three composition / tagging enhancements
driven by 1:1 matching against the existing DCAT3 catalog API. The
visible change in committed examples is the composition tag
inheritance — operations under `Book.authors` are now tagged
`Author` instead of `Book.authors`. Schemas without the new
annotations get the same path *set* as 0.6.1 plus the new tag
default.

### Added

- **`openapi.path_template` now emits a collection path alongside the
  item path** ([#44](https://github.com/jackhiggs/linkml-openapi/issues/44)).
  When the template ends with a ``/{name}`` segment the leaf is
  treated as the item path; the segment is stripped to form the
  collection path, which gets ``list`` / ``create`` operations from
  the class's ``openapi.operations``. Operation IDs are still
  suffixed ``_via_template`` for global uniqueness, and the
  collection drops the leaf-id parameter from its path-parameter
  list. Opt out with the new ``openapi.path_template_collection:
  "false"`` class annotation for genuinely item-only legacy URLs.
- **Multi-level composition chains.** When an ``inlined: true``
  composition target itself has ``inlined: true`` slots, the nested
  paths now chain through every level instead of stopping after one
  hop. So ``Catalog.datasets[FusionDataset].distributions[FusionDistribution]``
  emits ``/catalogs/{catalogId}/datasets/{datasetId}/distributions``
  and ``…/distributions/{distributionId}`` automatically — no need
  for the intermediate to be a resource in its own right. Mutual
  composition (``A.bs[B].as[A]``) is cycle-protected; the recursion
  walks one cycle around then short-circuits.
- **`openapi.tag` class annotation.** Overrides the ``tags`` value
  used in OpenAPI operations, controlling how Swagger UI groups
  endpoints. Default is the class name (current behaviour).
  **Composition- and reference-derived nested operations now inherit
  the *target* class's tag** (``Dataset``) rather than the previous
  ``Parent.slot`` form (``Catalog.datasets``), so all "Dataset"
  operations appear under one Swagger UI group regardless of where
  in the URL hierarchy they live. This is a visible change in the
  committed examples (`bookstore`, `petstore`); schemas without
  ``openapi.tag`` keep the new target-class-name default.

## [0.6.1] — 2026-04-28

Internal cleanup release driven by a code-reuse / quality / efficiency
review across the v0.4.0..v0.6.0 changeset. No behaviour change —
generated specs are byte-identical against every committed example —
but a few internal corners are cleaner and one latent correctness
issue is fixed.

### Fixed

- **Parent-chain cache was order-dependent.** ``_collect_parent_chains``
  memoised chains under one walk's ``on_path`` and reused them under
  another's, so a class reachable through two ancestor sub-graphs could
  show different chains depending on which leaf the top-level loop
  reached first. The cache is dropped — the relationship-graph walk is
  cheap on resource classes and now deterministic.

### Changed

- **Hot-path performance.** A per-build cache of induced slots indexed
  by ``(class_name, slot_name)`` collapses the previous O(slots²)
  lookups in ``_get_slot_annotation``, ``_render_slot_segment``, the
  nested-path emitters, and the chain builder to O(1). The
  ``_get_resource_classes`` result is now cached too — both
  ``_collect_parent_chains`` and ``_build_openapi`` need it.
- **DRY internals.** The triplicated ``openapi.path_id`` rename logic
  (flat item path, ``_make_nested_paths``, deep-chain emitter) is
  consolidated into a single ``_resolve_item_path_vars`` helper. The
  five-line CRUD-attach block (``if "read" in operations: …`` repeated
  across three emit-methods) is a single ``_attach_item_operations``
  call. The 15+ test methods that hand-rolled the temp-file +
  generate + cleanup boilerplate now use module-level
  ``_generate_from_string`` / ``_generate_from_string_raises``
  helpers.
- **Constants over magic strings.** Module-level ``PATH_STYLE_SNAKE``
  / ``PATH_STYLE_KEBAB`` / ``SUPPORTED_PATH_STYLES`` and
  ``OP_LIST`` / ``OP_CREATE`` / ``OP_READ`` / ``OP_UPDATE`` /
  ``OP_PATCH`` / ``OP_DELETE`` (plus ``DEFAULT_OPERATIONS``,
  ``ITEM_OPERATIONS``) replace the repeated string literals. The CLI
  ``--path-style`` choices are derived from the same constant so the
  list can't drift.
- Stale ``# (issue #N)`` markers removed from in-source section
  dividers — issue references belong in commit messages and the
  changelog, not in code that has to stay current.

## [0.6.0] — 2026-04-28

URL-shape and inheritance-correctness release. The new `openapi.path_style`
turns kebab-case URLs on with one annotation, and the `is_a` propagation
fix finally lets parent-class slot suppressions reach subclasses where
LinkML semantics already said they should.

### Fixed

- **Slot annotations now propagate through `is_a` inheritance**
  ([#40](https://github.com/jackhiggs/linkml-openapi/issues/40)).
  ``openapi.nested: "false"`` (and every other ``openapi.*`` slot
  annotation) declared on a parent class's ``slot_usage`` now reaches
  every subclass that induces the slot, instead of silently dropping
  at the inheritance boundary. ``_get_slot_annotation`` consulted only
  the class's direct ``slot_usage`` and the top-level slot definition;
  it now also reads the induced slot's annotations, which is where
  ``linkml-runtime`` deposits the merged ``slot_usage`` from the
  ``is_a`` chain. Direct slot_usage on the subclass still wins over
  inherited values (most-specific override semantics, same as LinkML).

### Added

- **Kebab-case URL paths**
  ([#38](https://github.com/jackhiggs/linkml-openapi/issues/38)).
  LinkML slot names must be valid identifiers (snake_case), but most
  REST APIs render URL segments in kebab-case. Two new annotations
  bridge the gap without changing slot identifiers in the OpenAPI body:

  - **Schema-level / CLI** ``openapi.path_style: "kebab-case"`` (or
    ``--path-style kebab-case``) flips the convention for every
    auto-derived URL segment in the spec — both class- and
    slot-driven, including chain-prefix segments inside deep paths.
    Default ``"snake_case"``.
  - **Slot-level** ``openapi.path_segment: "<segment>"`` overrides the
    rendered URL segment for one slot, taken verbatim regardless of
    the active style. Useful for literal segments like ``data.services``
    or legacy URL contracts.

  Slot identifiers in the OpenAPI body, operation IDs, tags, JSON
  property keys, ``x-rdf-property`` URIs, and path-template literals
  are unaffected — only URL path segments change. Default behaviour
  unchanged: schemas without the new annotations regenerate
  byte-identically.

## [0.5.0] — 2026-04-28

URL-shape and query-surface release: lets a single LinkML schema express
the canonical URL hierarchies that catalog APIs (DCAT3, FHIR, etc.)
expect, while giving lean control over which slots become list-endpoint
filters. All additive — schemas without the new annotations regenerate
byte-identically.

### Added

- **Layered opt-out for auto-inferred query parameters**
  ([#34](https://github.com/jackhiggs/linkml-openapi/issues/34)). Lean
  classes still get one auto-inferred query parameter per scalar slot;
  catalog-shaped classes with 30+ slots can now suppress the bloat with
  one annotation:

  - **Schema-level** ``openapi.auto_query_params: "false"`` flips the
    default for the whole spec.
  - **Class-level** ``openapi.auto_query_params: "false" | "true"``
    overrides the schema-level setting per class (so a single noisy
    class can opt out, or — when the schema-level default is off — a
    single class can opt back in).
  - **Slot-level** ``openapi.query_param: "false"`` excludes one slot
    from auto-inference even when auto is enabled.

  Default remains ``"true"``, so schemas without any of the new
  annotations regenerate byte-identically. ``limit`` / ``offset``
  always emit on list endpoints regardless of the setting.
- **Deep nested item paths via parent-chain walk** ([#32](https://github.com/jackhiggs/linkml-openapi/issues/32)).
  When a resource class is reachable from one or more ancestor resource
  classes via multivalued relationship slots, the generator now emits a
  deep item path that includes every ancestor's identifier as a path
  parameter. For ``Catalog.datasets: list[Dataset]`` and
  ``Dataset.distributions: list[Distribution]`` (all three resources),
  the canonical deep paths are::

      /catalogs/{catalogId}/datasets/{datasetId}
      /catalogs/{catalogId}/datasets/{datasetId}/distributions/{distId}

  Each ancestor's identifier becomes a URL parameter — *not* a field on
  the leaf component schema. Operation IDs on deep paths are suffixed
  ``_via_<chain>`` so they remain globally unique alongside the
  flat-path operations.
- **`openapi.path_id` class annotation** — overrides the default
  ``<class_snake>_id`` URL parameter name everywhere the class appears
  in a URL (its own flat item path, single-level nested item paths
  pointing to it, and ancestor segments in deep chains). Set to
  ``catalogId`` to emit ``{catalogId}`` instead of the default
  ``{catalog_id}``. Existing schemas without the annotation keep
  byte-identical output.
- **`openapi.parent_path` class annotation** — picks the canonical
  chain when a leaf class is reachable via multiple ancestor chains.
  Accepts ``/``-separated hops; each hop is either ``slot_name``
  (when unambiguous) or ``ClassName.slot_name`` (when class qualifier
  is needed to disambiguate). Without the annotation, an ambiguous
  leaf raises at generation time with the candidate chains listed.
- **`openapi.nested_only` class annotation** — drops the flat
  ``/<class>`` and ``/<class>/{id}`` paths so the deep nested URL is
  the only canonical surface for a class. Pairs naturally with
  ``openapi.parent_path`` for sub-resources that don't make sense on
  their own.
- **`openapi.flat_only` class annotation**
  ([#36](https://github.com/jackhiggs/linkml-openapi/issues/36))
  — converse of ``openapi.nested_only``. Drops the deep nested item
  path emission for the class while keeping the flat collection +
  flat item paths. Single-level nested paths under a parent (which
  are about this class as a *parent*, not as a leaf) still emit.
  Setting both ``openapi.nested_only`` and ``openapi.flat_only`` on
  the same class is a generation error.
- **`openapi.path_template` + `openapi.path_param_sources`
  class annotations** (Layer 4 escape hatch,
  [#36](https://github.com/jackhiggs/linkml-openapi/issues/36)) —
  hand-authored URL template that replaces the auto-derived deep
  chain. Used for legacy contracts the relationship graph can't
  express (literal segments like ``by-doi``, compound keys,
  version prefixes). Each ``{name}`` placeholder must be paired
  with a ``name:Class.slot`` source so parameter schemas remain
  typed. Validates: placeholder set matches source-key set
  exactly, every ``Class.slot`` resolves, and operation IDs are
  suffixed ``_via_template`` to stay globally unique.

## [0.4.0] — 2026-04-27

Dependency-surface release: cuts the install tree down to `linkml-runtime`
and a handful of small, permissively-licensed transitives so the package
clears strict corporate licence-scanning policies. No behaviour change —
generated specs are byte-identical against every committed example, and
the public Python and CLI surfaces are unchanged apart from the removal
of seven CLI flags that were always no-ops in this generator.

### Changed

- **Dropped `linkml` as a runtime dependency.** The package now relies on
  `linkml-runtime` alone, with a minimal `linkml_openapi._base.Generator`
  shim replacing the upstream `linkml.utils.generator.Generator` base
  class. The OpenAPI generator only ever used the SchemaView-based
  new-style path (`uses_schemaloader = False`); the SchemaLoader visitor
  pattern, the legacy CLI options (`--useuris`, `--importmap`,
  `--mergeimports`, `--log_level`, `--verbose`, `--stacktrace`,
  `--metadata`), and the SchemaLoader-only fields (`base_dir`,
  `namespaces`, `metamodel`) were never read.
- **Effect on transitive dependencies.** Removing the `linkml`
  distribution drops `pyshex` / `pyshexc` (which pulled in `rfc3987`,
  GPL-licensed), `sphinx-click` (which pulled in `docutils`),
  `SQLAlchemy` (which pulled in `greenlet`), and `linkml-dataops` /
  `jsonpatch` (which pulled in `jsonpointer`) from the install tree.
  Generated specs are byte-identical against every committed example.
- The `linkml.generators` plugin entry point is preserved so users who
  also have `linkml` installed can keep invoking the unified
  `linkml`/`gen-linkml` CLIs against this generator.

## [0.3.0] — 2026-04-26

This release adds the annotations and CLI surface needed to drive a
realistic catalog API (DCAT3, FOAF, internal/partner/external splits)
from a single LinkML schema. New annotations are listed alongside each
feature; new CLI flags are summarised at the end.

### Added

- **RFC 7807 error model.** Non-2xx responses reference a `Problem`
  schema by default. Override the schema name via the
  `openapi.error_class` schema-level annotation, or disable emission
  entirely with `--no-error-schema`.
  ([#14](https://github.com/jackhiggs/linkml-openapi/issues/14))
- **Composition vs. reference nested paths.** Nested resources whose
  range is a `inlined: true` (composition) class are inlined into the
  parent's request/response bodies; nested resources whose range is a
  class with its own `identifier` (reference) emit child paths under
  the parent. Use the `openapi.nested: "false"` slot annotation to
  opt a slot out of the nested-path emission while keeping the
  property in the schema body.
  ([#18](https://github.com/jackhiggs/linkml-openapi/issues/18))
- **Discriminator / polymorphism.** Subclass schemas under an abstract
  parent emit an OpenAPI `discriminator` block. Two signals are
  honoured: LinkML-native `designates_type: true` on a slot, and the
  `openapi.discriminator: <field>` class annotation paired with
  per-subclass `openapi.type_value: <VALUE>` to keep custom field
  names and uppercase enum values used by existing systems.
  ([#20](https://github.com/jackhiggs/linkml-openapi/issues/20))
- **PATCH operations.** `openapi.operations` accepts `patch` in
  addition to the existing tokens. Generated PATCH bodies use
  `application/merge-patch+json` (RFC 7396) and reuse the resource
  schema with all properties optional.
  ([#16](https://github.com/jackhiggs/linkml-openapi/issues/16))
- **Inverse-direction nested paths.** When a slot declares
  `inverse: <Class>.<slot>` and the named inverse slot is missing on
  the target class, the generator synthesises the reverse-direction
  path so the consumer can still navigate the relationship from
  either end.
  ([#19](https://github.com/jackhiggs/linkml-openapi/issues/19))
- **Query operator grammar.** `openapi.query_param` now accepts a
  comma-separated set of tokens instead of a single string. `sortable`
  emits an `order` query parameter; `comparable` emits `<slot>__gt` /
  `<slot>__gte` / `<slot>__lt` / `<slot>__lte` query parameters with
  the slot's range. Unknown tokens warn at generation time so typos
  surface early.
  ([#15](https://github.com/jackhiggs/linkml-openapi/issues/15))
- **Profiles for multi-view generation.** `--profile <NAME>` filters
  the spec to a named view configured via flat-dotted schema-level
  annotations: `openapi.profile.<NAME>.exclude_classes`,
  `openapi.profile.<NAME>.exclude_slots`, and
  `openapi.profile.<NAME>.description`. Run the generator multiple
  times against one LinkML schema to publish internal, partner, and
  external surfaces from a single source of truth.
  ([#17](https://github.com/jackhiggs/linkml-openapi/issues/17))

### Changed

- Post-merge cleanup of the v0.2.0..main changeset
  ([#28](https://github.com/jackhiggs/linkml-openapi/pull/28)):
  precomputed synthetic-inverse index (O(N²) → O(N)), single-pass
  query-param emission, shared `_parse_csv` helper, profile-filter
  drift detection folded into resolution, and a small set of
  comment / docstring fixes flagged by the simplify review.

### CLI

- New flags: `--profile NAME`, `--error-schema` /
  `--no-error-schema`. Existing flags
  (`--openapi-version`, `--flatten-inheritance`, `--api-title`,
  `--api-version`, `--server-url`, `--classes`) are unchanged.

## [0.2.0] — 2026-04-25

### Added

- `openapi.media_types` class annotation: every operation generated for a
  class advertises every listed media type on its responses and request
  bodies. Default is `application/json`. ([#1](https://github.com/jackhiggs/linkml-openapi/issues/1))
- `x-rdf-class` and `x-rdf-property` extensions: LinkML's `class_uri` and
  `slot_uri` are propagated into the generated OpenAPI as standard `x-`
  extensions, with CURIEs expanded against the schema's `prefixes` map.
  RDF-aware downstream tooling can now consume the OpenAPI spec directly
  without re-parsing the LinkML schema. ([#2](https://github.com/jackhiggs/linkml-openapi/issues/2))
- `--openapi-version` flag (`3.0.3` / `3.1.0`) and `--flatten-inheritance`
  flag for downstream-codegen friendliness. ([#3](https://github.com/jackhiggs/linkml-openapi/issues/3))
- `openapi.format` slot annotation overrides the OpenAPI `format` string
  per slot — for `int64`, `binary`, `byte`, `password`, etc. ([#4](https://github.com/jackhiggs/linkml-openapi/issues/4))
- `openapi.path_variable` accepts `"slug"` / `"iri"` (`"true"` is an alias
  for `"iri"`). `"slug"` emits `string` regardless of slot range. ([#5](https://github.com/jackhiggs/linkml-openapi/issues/5))
- Pluralization handles `-ch` / `-sh` and treats `series` / `species` /
  `genus` as invariant. Warns at generation time for irregular Latin /
  Greek loanwords (`Datum`, `Criterion`, `Analysis`, …) so the user can
  set `openapi.path` explicitly. ([#6](https://github.com/jackhiggs/linkml-openapi/issues/6))
- `.github/dependabot.yml` for weekly pip + github-actions updates.

### Changed

- **Default `openapi:` version flipped from `3.1.0` to `3.0.3`.** The
  spec body is structurally identical between the two dialects in this
  generator's output; the change is purely the version string. Pass
  `--openapi-version 3.1.0` to opt back into the newer dialect once
  downstream tooling catches up. The motivating issue is
  `openapi-generator`'s Spring codegen, which mishandles 3.1 + `allOf`
  inheritance and synthesises spurious duplicate schemas.
- `generatorversion`, `cli --version`, `__version__`, and
  `pyproject.toml`'s `version` are now sourced from a single place.

## [0.1.2] — 2026-02-16

Initial public release.

[0.8.2]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.8.2
[0.8.1]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.8.1
[0.8.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.8.0
[0.7.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.7.0
[0.6.1]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.6.1
[0.6.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.6.0
[0.5.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.5.0
[0.4.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.4.0
[0.3.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.3.0
[0.2.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.2.0
[0.1.2]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.1.2
