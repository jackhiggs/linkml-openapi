"""CLI for generating OpenAPI specs from LinkML schemas."""

import click
from linkml.utils.generator import shared_arguments

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
@click.version_option("0.1.0", "-V", "--version")
def cli(yamlfile, resource_filter=(), **kwargs):
    """Generate OpenAPI 3.1 specification from a LinkML schema."""
    resource_filter = list(resource_filter) if resource_filter else None
    gen = OpenAPIGenerator(yamlfile, resource_filter=resource_filter, **kwargs)
    click.echo(gen.serialize())


def main():
    cli()


if __name__ == "__main__":
    main()
