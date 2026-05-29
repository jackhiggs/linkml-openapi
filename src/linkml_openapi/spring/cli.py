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
@click.option(
    "--error-responses",
    "error_responses",
    default=None,
    metavar="CODES",
    help=(
        "Comma-separated list of extra HTTP error status codes "
        "(4xx/5xx) to declare on every controller `@ApiResponse` "
        "block. Overrides the schema-level `openapi.error_responses` "
        "annotation. Each code is described with its standard reason "
        "phrase and references the active error class (Problem by "
        "default). Threaded through to the sidecar OpenAPI spec so "
        "springdoc's runtime view matches."
    ),
)
@click.option(
    "--profile",
    default=None,
    metavar="NAME",
    help=(
        "Active profile name for the sidecar OpenAPI spec. The "
        "profile must be declared via `openapi.profile.<NAME>.<key>` "
        "schema annotations; classes / slots listed in `exclude_*` / "
        "`include_*` are filtered out of the sidecar. Mirrors "
        "`gen-openapi --profile`. NOTE: profile filtering applies "
        "only to the sidecar spec — the Spring controllers are "
        "always emitted from the full schema. Use this when the "
        "Spring service hosts the full API surface but the sidecar "
        "needs a narrower partner/external view."
    ),
)
@click.option(
    "--emit-namespaces",
    "emit_namespaces",
    is_flag=True,
    default=False,
    help=(
        "Emit a top-level `x-namespaces` map on the sidecar OpenAPI "
        "spec (CURIE prefix → expanded IRI) drawn from the LinkML "
        "schema's `prefixes:` block. Lets RDF runtimes build "
        "JSON-LD `@context` blocks / Turtle `@prefix` declarations "
        "from a single source of truth. Mirrors `gen-openapi "
        "--emit-namespaces`. Defaults to off."
    ),
)
@click.option(
    "--rdf-resolved-map",
    "rdf_resolved_map",
    is_flag=True,
    default=False,
    help=(
        "Emit `x-rdf-properties-resolved` + `x-ranges-resolved` on "
        "every component schema in the sidecar — slot name → "
        "expanded RDF predicate IRI / resolved range class names, "
        "with inheritance via `allOf` already resolved. Mirrors "
        "`gen-openapi --rdf-resolved-map`. Defaults to off."
    ),
)
@click.option(
    "--post-process",
    "post_process",
    default=None,
    metavar="NAME[,NAME...]",
    help=(
        "Comma-separated list of registered post-processors to apply "
        "to the sidecar OpenAPI spec. Mirrors `gen-openapi "
        "--post-process`. See linkml_openapi.post_processors for the "
        "registry."
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
    error_responses: str | None,
    profile: str | None,
    emit_namespaces: bool,
    rdf_resolved_map: bool,
    post_process: str | None,
) -> None:
    """Generate Spring server source files directly from a LinkML schema."""
    parsed_codes: list[int] | None = None
    if error_responses is not None:
        parsed_codes = []
        for token in error_responses.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                parsed_codes.append(int(token))
            except ValueError as exc:
                raise click.BadParameter(f"--error-responses: {token!r} is not an integer") from exc
    post_processors: list[str] = []
    if post_process:
        post_processors = [n.strip() for n in post_process.split(",") if n.strip()]
    gen = SpringServerGenerator(
        yamlfile,
        package=package,
        path_prefix=path_prefix,
        path_style=path_style,
        reactive=reactive,
        error_responses=parsed_codes,
        sidecar_profile=profile,
        sidecar_emit_namespaces=emit_namespaces,
        sidecar_rdf_resolved_map=rdf_resolved_map,
        sidecar_post_processors=post_processors,
    )
    written = gen.emit(output)
    for path in written:
        click.echo(str(path))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
