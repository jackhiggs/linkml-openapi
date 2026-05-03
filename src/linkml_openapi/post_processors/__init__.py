"""Spec post-processors that rewrite the generated OpenAPI dict.

Each post-processor is a pure function ``dict -> dict`` (in-place
mutation is fine; return is for chaining). They run after
:meth:`OpenAPIGenerator.serialize` builds the spec but before
serialisation, in the order the user supplies via
``--post-process NAME[,NAME...]`` on the CLI.

The split between post-processors and the core generator is
deliberate: the generator emits a *canonical* spec optimised for
authoring clarity (allOf inheritance, inline oneOfs at use sites,
RDF metadata), and post-processors adapt that canonical form to
specific consumer needs (openapi-generator codegens, validator
quirks, JSON-LD adapters, etc.). Adding a new post-processor never
requires touching the generator.
"""

from collections.abc import Callable

from .extract_inline_oneof import extract_inline_oneof

PostProcessor = Callable[[dict], dict]

REGISTRY: dict[str, PostProcessor] = {
    "extract-inline-oneof": extract_inline_oneof,
}


def apply(spec: dict, names: list[str]) -> dict:
    """Run a sequence of registered post-processors against ``spec``.

    ``names`` come from the CLI in declared order. Unknown names raise
    ``ValueError`` so typos surface immediately rather than silently
    skipping a transformation.
    """
    for name in names:
        processor = REGISTRY.get(name)
        if processor is None:
            available = ", ".join(sorted(REGISTRY))
            raise ValueError(
                f"Unknown post-processor {name!r}. "
                f"Available: {available}"
            )
        spec = processor(spec)
    return spec


__all__ = ["apply", "REGISTRY", "extract_inline_oneof"]
