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
@click.option(
    "--path-style",
    type=click.Choice(["snake_case", "kebab-case"]),
    default=None,
    help=(
        "URL path-segment convention. Defaults to the schema-level "
        "`openapi.path_style` annotation, or `snake_case` if neither is "
        "set (byte-identical to today). `kebab-case` flips auto-derived "
        "class- and slot-driven URL segments to hyphenated form. "
        "Threads through to controller `@*Mapping` URLs and the sidecar "
        "OpenAPI spec so springdoc's runtime view matches."
    ),
)
@click.option(
    "--path-prefix",
    default=None,
    metavar="/PREFIX",
    help=(
        "URL path prefix prepended to every controller. Defaults to the "
        "schema-level `openapi.path_prefix` annotation; CLI flag wins. "
        'Emitted as a class-level `@RequestMapping(value = "<prefix>")` '
        "on every controller interface — method-level mappings stay "
        "relative. The sidecar `resources/openapi.yaml` adopts the same "
        "prefix on its `paths:` keys to match springdoc's runtime view."
    ),
)
@click.option(
    "--reactive/--no-reactive",
    "reactive",
    default=None,
    help=(
        "Emit Spring WebFlux (reactive) controllers instead of blocking "
        "Spring MVC. Wraps every return type in `Mono<...>`, list endpoints "
        "in `Mono<ResponseEntity<Flux<T>>>`, `@RequestBody` parameters in "
        "`Mono<...>`; imports `reactor.core.publisher.{Mono,Flux}`. "
        "The sidecar OpenAPI spec is unchanged — reactive is purely a "
        "codegen concern (mirrors openapi-generator's `reactive: true` "
        "Spring template). Defaults to the schema-level `openapi.reactive` "
        "annotation, or off (today's blocking output) if neither is set. "
        "Reactive apps depend on `spring-boot-starter-webflux` instead of "
        "`spring-boot-starter-web`."
    ),
)
@click.version_option(__version__, "-V", "--version")
def cli(
    yamlfile,
    output: Path,
    package: str,
    path_prefix: str | None,
    path_style: str | None,
    reactive: bool | None,
) -> None:
    """Generate Spring server source files directly from a LinkML schema."""
    gen = SpringServerGenerator(
        yamlfile,
        package=package,
        path_prefix=path_prefix,
        path_style=path_style,
        reactive=reactive,
    )
    written = gen.emit(output)
    for path in written:
        click.echo(str(path))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
