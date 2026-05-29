"""Shared pytest fixtures for the linkml-openapi test suite.

The ``person_spec`` fixture is the dominant performance lever — the
suite calls ``_generate()`` (no kwargs) against ``person.yaml`` from
~50 test methods, each of which previously instantiated a fresh
``OpenAPIGenerator``, walked the schema, and serialised the spec
(~0.8s each). The module-scoped fixture below caches the result for
the duration of a test module so the cost is paid once.

Tests that need a non-default generator configuration still pass
kwargs through ``_make_generator`` / ``_generate``; only the bare
no-kwarg path benefits from caching.

The ``schema_to_file`` helper centralises the
``tempfile.NamedTemporaryFile`` boilerplate that ~30 tests had
duplicated inline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest
import yaml

from linkml_openapi.generator import OpenAPIGenerator

FIXTURES = Path(__file__).parent / "fixtures"
PERSON_SCHEMA = str(FIXTURES / "person.yaml")


@pytest.fixture(scope="module")
def person_spec() -> dict:
    """Cached ``person.yaml`` OpenAPI spec for tests that don't pass
    kwargs. Module-scoped so the first call in a test file pays the
    walk/serialise cost; subsequent tests in the same module reuse
    the result.
    """
    raw = OpenAPIGenerator(PERSON_SCHEMA).serialize(format="yaml")
    return yaml.safe_load(raw)


@pytest.fixture(scope="module")
def person_spec_json() -> dict:
    """JSON-format ``person.yaml`` OpenAPI spec, module-scoped."""
    raw = OpenAPIGenerator(PERSON_SCHEMA).serialize(format="json")
    return json.loads(raw)


@pytest.fixture
def schema_to_file(tmp_path: Path) -> Callable[[str], str]:
    """Write a YAML schema string to a temp file and return its
    path. Replaces the ~30 inline ``tempfile.NamedTemporaryFile``
    blocks that test methods used to set up ad-hoc schemas.

    Uses ``tmp_path`` instead of ``tempfile`` so pytest handles
    cleanup automatically — no leaked files in ``/tmp`` on test
    crashes (the prior pattern used ``delete=False`` and a
    ``try/finally`` unlink that ran only on the happy path).
    """

    counter = {"n": 0}

    def _write(schema_yaml: str) -> str:
        counter["n"] += 1
        path = tmp_path / f"schema_{counter['n']}.yaml"
        path.write_text(schema_yaml)
        return str(path)

    return _write
