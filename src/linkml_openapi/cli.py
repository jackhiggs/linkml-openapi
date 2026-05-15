"""CLI for generating OpenAPI specs from LinkML schemas."""

from pathlib import Path

import click

from linkml_openapi import __version__
from linkml_openapi.generator import SUPPORTED_PATH_STYLES, OpenAPIGenerator


@click.command(name="gen-openapi")
@click.argument("yamlfile", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--format",
    "-f",
    type=click.Choice(OpenAPIGenerator.valid_formats),
    default=OpenAPIGenerator.valid_formats[0],
    show_default=True,
    help="Output format.",
)
@click.option("--api-title", help="API title (default: schema name)")
@click.option("--api-version", default="1.0.0", show_default=True, help="API version")
@click.option(
    "--server-url",
    default="http://localhost:8000",
    show_default=True,
    help="Server base URL",
)
@click.option(
    "--classes",
    "resource_filter",
    multiple=True,
    help="Only generate endpoints for these classes (repeatable)",
)
@click.option(
    "--openapi-version",
    type=click.Choice(["3.0.3", "3.1.0"]),
    default="3.0.3",
    show_default=True,
    help="OpenAPI dialect to emit. 3.0.3 is the default for downstream codegen compatibility.",
)
@click.option(
    "--flatten-inheritance",
    is_flag=True,
    default=False,
    help="Inline parent properties into subclass schemas instead of using allOf.",
)
@click.option(
    "--error-schema/--no-error-schema",
    "error_schema",
    default=True,
    show_default=True,
    help=(
        "Emit an RFC 7807 Problem schema and reference it from non-2xx "
        "response bodies. Override the schema with the openapi.error_class "
        "schema annotation."
    ),
)
@click.option(
    "--profile",
    default=None,
    metavar="NAME",
    help=(
        "Active profile name. The profile must be declared via "
        "`openapi.profile.<NAME>.<key>` schema annotations; classes / "
        "slots listed in `exclude_classes` / `exclude_slots` are filtered "
        "out of the generated spec. Use this to drive multiple API "
        "surfaces (internal, partner, external) from one LinkML schema."
    ),
)
@click.option(
    "--path-style",
    type=click.Choice(sorted(SUPPORTED_PATH_STYLES)),
    default=None,
    help=(
        "URL path-segment convention. Defaults to the schema-level "
        "`openapi.path_style` annotation, or `snake_case` if neither is set "
        "(byte-identical to today). `kebab-case` renders auto-derived "
        "class- and slot-driven URL segments with `-` instead of `_`. Slot "
        "identifiers in the OpenAPI body, operation IDs, and RDF extensions "
        "are unaffected."
    ),
)
@click.option(
    "--path-prefix",
    default=None,
    metavar="/PREFIX",
    help=(
        "URL path prefix prepended to every emitted `paths:` key "
        "(e.g. `/api/v1`). Defaults to the schema-level "
        "`openapi.path_prefix` annotation; CLI flag wins. Validation "
        "errors if a class's `openapi.path` already starts with the "
        "prefix (would double up). `servers[0].url` is left alone — "
        "pass `--server-url` separately if you want the prefix in the "
        "server URL too."
    ),
)
@click.option(
    "--emit-name-mappings",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    metavar="PATH",
    help=(
        "Write the wire→codegen rename map to PATH for use with "
        "`openapi-generator-cli --name-mappings @PATH`. Captures any "
        "`openapi.legacy_type_codegen_name` (and similar) annotations "
        "from the schema. No file is written when the schema declares "
        "no renames."
    ),
)
@click.option(
    "--codegen-friendly",
    is_flag=True,
    default=False,
    help=(
        "Emit a spec optimised for downstream codegens (openapi-generator, "
        "NSwag, etc.). Drops the single-value `enum` on every discriminator "
        "subclass schema (keeps `default` only) so codegens don't generate "
        "single-value enum classes; replaces inline `oneOf` at use sites "
        "with `$ref` to the polymorphic parent (the parent's component "
        "schema gains its own `discriminator` block + `mapping` so dispatch "
        "still works)."
    ),
)
@click.option(
    "--rdf-resolved-map",
    "rdf_resolved_map",
    is_flag=True,
    default=False,
    help=(
        "Emit a flattened `x-rdf-properties-resolved` map on every "
        "component schema — slot name → expanded RDF predicate IRI, "
        "with inheritance via `allOf` already resolved. Lets RDF "
        "runtimes (spring-rdf, etc.) look up any field's predicate "
        "directly on the subclass schema without chasing `$ref` chains. "
        "Per-property `x-rdf-property` annotations are emitted in both "
        "modes (back-compat). Also emits an `x-ranges-resolved` map "
        "(slot name → list of resolved range class names) so runtimes "
        "can deserialise into typed objects without walking `allOf` / "
        "`oneOf`. Defaults to off — schemas regenerate byte-identically "
        "when unset."
    ),
)
@click.option(
    "--emit-namespaces",
    "emit_namespaces",
    is_flag=True,
    default=False,
    help=(
        "Emit a top-level `x-namespaces` map on the spec (CURIE prefix "
        "→ expanded IRI) drawn from the LinkML schema's `prefixes:` "
        "block. Lets RDF runtimes build JSON-LD `@context` blocks / "
        "Turtle `@prefix` declarations / RDF-XML namespaces from a "
        "single source of truth without re-parsing the LinkML schema. "
        "Defaults to off — schemas regenerate byte-identically when "
        "unset."
    ),
)
@click.option(
    "--post-process",
    "post_process",
    default=None,
    metavar="NAME[,NAME...]",
    help=(
        "Comma-separated list of registered post-processors to apply "
        "to the generated spec, in order. See "
        "linkml_openapi.post_processors for the registry. Example: "
        "--post-process extract-inline-oneof"
    ),
)
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, resource_filter=(), emit_name_mappings=None, post_process=None, **kwargs):
    """Generate an OpenAPI specification from a LinkML schema."""
    resource_filter = list(resource_filter) if resource_filter else None
    if post_process:
        kwargs["post_processors"] = [n.strip() for n in post_process.split(",") if n.strip()]
    gen = OpenAPIGenerator(yamlfile, resource_filter=resource_filter, **kwargs)
    spec = gen.serialize()
    click.echo(spec)
    if emit_name_mappings is not None:
        content = gen.emit_name_mappings()
        if content:
            emit_name_mappings.write_text(content)


def main():
    cli()


if __name__ == "__main__":
    main()
