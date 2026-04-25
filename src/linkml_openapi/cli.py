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
@click.version_option("0.1.0", "-V", "--version")
def cli(yamlfile, resource_filter=(), **kwargs):
    """Generate an OpenAPI specification from a LinkML schema."""
    resource_filter = list(resource_filter) if resource_filter else None
    gen = OpenAPIGenerator(yamlfile, resource_filter=resource_filter, **kwargs)
    click.echo(gen.serialize())


def main():
    cli()


if __name__ == "__main__":
    main()
