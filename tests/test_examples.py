"""Verify that committed example OpenAPI outputs stay in sync with their schemas."""

from pathlib import Path

import pytest
import yaml

from linkml_openapi.generator import OpenAPIGenerator

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

EXAMPLE_DIRS = sorted(
    d for d in EXAMPLES_DIR.iterdir() if d.is_dir() and (d / "schema.yaml").exists()
)


@pytest.mark.parametrize(
    "example_dir",
    EXAMPLE_DIRS,
    ids=[d.name for d in EXAMPLE_DIRS],
)
def test_example_output_matches(example_dir: Path) -> None:
    schema_path = example_dir / "schema.yaml"
    expected_path = example_dir / "openapi.yaml"

    gen = OpenAPIGenerator(str(schema_path))
    actual = yaml.safe_load(gen.serialize(format="yaml"))
    expected = yaml.safe_load(expected_path.read_text())

    assert actual == expected, (
        f"Generated output for {example_dir.name} does not match committed openapi.yaml. "
        f"Run 'bash examples/generate.sh' to update."
    )
