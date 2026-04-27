# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(while pre-1.0, minor bumps may carry visible behaviour changes).

## Unreleased

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

[0.4.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.4.0
[0.3.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.3.0
[0.2.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.2.0
[0.1.2]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.1.2
