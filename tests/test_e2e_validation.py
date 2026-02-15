"""End-to-end tests: OpenAPI spec validation and downstream code generation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from openapi_spec_validator import validate

from linkml_openapi.generator import OpenAPIGenerator

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

EXAMPLE_DIRS = sorted(
    d for d in EXAMPLES_DIR.iterdir() if d.is_dir() and (d / "schema.yaml").exists()
)

SCHEMA_PATHS = [
    *[d / "schema.yaml" for d in EXAMPLE_DIRS],
    FIXTURES_DIR / "person.yaml",
]

SCHEMA_IDS = [
    *[f"example:{d.name}" for d in EXAMPLE_DIRS],
    "fixture:person",
]


# ---------------------------------------------------------------------------
# Spec validation (no external tools)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=SCHEMA_IDS)
def test_generated_spec_is_valid_openapi(schema_path: Path) -> None:
    """Generated output passes OpenAPI 3.1 structural validation."""
    gen = OpenAPIGenerator(str(schema_path))
    spec = yaml.safe_load(gen.serialize(format="yaml"))
    validate(spec)


@pytest.mark.parametrize(
    "example_dir",
    EXAMPLE_DIRS,
    ids=[d.name for d in EXAMPLE_DIRS],
)
def test_committed_golden_file_is_valid_openapi(example_dir: Path) -> None:
    """Committed openapi.yaml golden files pass OpenAPI 3.1 validation."""
    spec = yaml.safe_load((example_dir / "openapi.yaml").read_text())
    validate(spec)


# ---------------------------------------------------------------------------
# TypeScript type generation (requires npx)
# ---------------------------------------------------------------------------

_has_npx = shutil.which("npx") is not None


@pytest.mark.e2e
@pytest.mark.skipif(not _has_npx, reason="npx not available")
@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=SCHEMA_IDS)
def test_typescript_codegen(schema_path: Path, tmp_path: Path) -> None:
    """openapi-typescript can generate types from the spec."""
    gen = OpenAPIGenerator(str(schema_path))
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(gen.serialize(format="yaml"))

    output_file = tmp_path / "types.ts"
    result = subprocess.run(
        ["npx", "-y", "openapi-typescript", str(spec_file), "-o", str(output_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"openapi-typescript failed:\n{result.stderr}"
    assert output_file.exists() and output_file.stat().st_size > 0


# ---------------------------------------------------------------------------
# Python client generation (requires docker)
# ---------------------------------------------------------------------------

_has_docker = shutil.which("docker") is not None


@pytest.mark.e2e
@pytest.mark.skipif(not _has_docker, reason="docker not available")
@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=SCHEMA_IDS)
def test_python_client_codegen(schema_path: Path, tmp_path: Path) -> None:
    """openapi-generator-cli can produce a Python client from the spec."""
    gen = OpenAPIGenerator(str(schema_path))
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(gen.serialize(format="yaml"))

    output_dir = tmp_path / "python-client"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{tmp_path}:/work",
            "openapitools/openapi-generator-cli",
            "generate",
            "-i",
            "/work/openapi.yaml",
            "-g",
            "python",
            "-o",
            "/work/python-client",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"openapi-generator failed:\n{result.stderr}"
    assert (output_dir / "setup.py").exists() or (output_dir / "pyproject.toml").exists()
