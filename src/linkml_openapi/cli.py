"""CLI for generating OpenAPI specs from LinkML schemas."""

import click
from linkml.utils.generator import shared_arguments

from linkml_openapi import __version__
from linkml_openapi.generator import OpenAPIGenerator


@shared_arguments(OpenAPIGenerator)
@click.command(name="gen-openapi")
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
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, resource_filter=(), **kwargs):
    """Generate an OpenAPI specification from a LinkML schema."""
    resource_filter = list(resource_filter) if resource_filter else None
    gen = OpenAPIGenerator(yamlfile, resource_filter=resource_filter, **kwargs)
    click.echo(gen.serialize())


def main():
    cli()


if __name__ == "__main__":
    main()
