# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(while pre-1.0, minor bumps may carry visible behaviour changes).

## Unreleased

### Added

- **Nested sub-resource paths from relationship slots.** Slots whose range
  is itself a resource class now produce `/<parent>/{id}/<slot>` endpoints
  alongside the existing flat collection paths. Multivalued slots emit a
  list endpoint with `limit` / `offset` and a `404` for missing parents;
  single-valued slots emit a single-object `GET`. The parent's path
  parameters are carried through, and the parent's `openapi.media_types`
  apply to the nested response.
- `openapi.nested: "false"` slot annotation suppresses one sub-resource
  path; `openapi.nest_subresources: "false"` class annotation suppresses
  every nested path under a parent class. Default is to nest.

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

[0.2.0]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.2.0
[0.1.2]: https://github.com/jackhiggs/linkml-openapi/releases/tag/v0.1.2
