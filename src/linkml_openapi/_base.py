"""Minimal Generator base used by ``linkml_openapi.generator.OpenAPIGenerator``.

This shim replaces ``linkml.utils.generator.Generator`` so the package can
depend on ``linkml-runtime`` alone. The full ``linkml`` distribution drags in
``pyshex`` (``rfc3987``, GPL), ``sphinx-click`` (``docutils``), ``SQLAlchemy``
(``greenlet``), and ``linkml-dataops``/``jsonpatch`` (``jsonpointer``) — none
of which the OpenAPI generator exercises. Decoupling here clears those
licence / dependency-scanning flags without changing any generator behaviour.

The shim keeps the contract OpenAPIGenerator already relied on:

* the dataclass declares ``schema``, ``format``, ``output`` as the first
  three fields, so ``OpenAPIGenerator(yamlfile, format=…)`` keeps working;
* ``__post_init__`` validates ``format`` against ``valid_formats`` and
  builds ``self.schemaview`` from a ``SchemaView``;
* ClassVars (``valid_formats``, ``file_extension``, ``generatorname``,
  ``generatorversion``, ``uses_schemaloader``) match the upstream names so
  the LinkML plugin entry point keeps resolving for users who still have
  the linkml CLI installed.

Legacy options (``importmap``, ``base_dir``, ``useuris``, ``mergeimports``,
``log_level``, ``verbose``, ``stacktrace``, ``metadata``) are dropped — the
OpenAPI generator never read them, and the SchemaLoader visitor pattern
they steered was already disabled (``uses_schemaloader = False``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from linkml_runtime.utils.schemaview import SchemaView


@dataclass
class Generator:
    """SchemaView-only Generator base for linkml-openapi."""

    schema: str | None = None
    """Path to a LinkML schema file, or YAML/JSON schema source."""

    format: str | None = None
    """Output format. Defaults to ``valid_formats[0]`` when unset."""

    output: str | None = None
    """Optional output path. The OpenAPI generator ignores this and lets the
    CLI handle redirection — kept for parity with the upstream signature."""

    valid_formats: ClassVar[list[str]] = []
    file_extension: ClassVar[str] = ""
    generatorname: ClassVar[str] = ""
    generatorversion: ClassVar[str] = ""
    uses_schemaloader: ClassVar[bool] = False

    def __post_init__(self) -> None:
        if not self.valid_formats:
            raise ValueError(
                f"{type(self).__name__} must declare valid_formats "
                "(ClassVar[list[str]]) before instantiation"
            )
        if self.format is None:
            self.format = self.valid_formats[0]
        if self.format not in self.valid_formats:
            raise ValueError(f"Unrecognized format: {self.format!r}; known={self.valid_formats}")
        # SchemaView already accepts the full union (path, YAML/JSON source,
        # SchemaDefinition, file-like) that the upstream Generator did, so
        # no wrapping is needed here.
        self.schemaview: SchemaView = SchemaView(self.schema)
