"""CLI entry point for the direct Spring server emitter."""

from pathlib import Path

import click

from linkml_openapi import __version__
from linkml_openapi.spring.generator import SpringServerGenerator


@click.command(name="gen-spring-server")
@click.argument("yamlfile", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory under which the Java source tree is written.",
)
@click.option(
    "--package",
    default="io.example",
    show_default=True,
    help=(
        "Java root package. Models go in <package>.model, controller interfaces in <package>.api."
    ),
)
@click.version_option(__version__, "-V", "--version")
def cli(yamlfile, output: Path, package: str) -> None:
    """Generate Spring server source files directly from a LinkML schema."""
    gen = SpringServerGenerator(yamlfile, package=package)
    written = gen.emit(output)
    for path in written:
        click.echo(str(path))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
