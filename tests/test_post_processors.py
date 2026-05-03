"""Tests for the spec post-processor framework.

Each post-processor is a pure ``dict -> dict`` function. Tests are
input/output dict pairs — no LinkML required, no generator
required. Keeps the post-processor surface independently testable
and lets us pin specific transformations (hoist behaviour, dedup,
naming) with surgical assertions.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from linkml_openapi.generator import OpenAPIGenerator
from linkml_openapi.post_processors import REGISTRY, apply
from linkml_openapi.post_processors.extract_inline_oneof import (
    extract_inline_oneof,
)


class TestRegistry:
    def test_known_post_processors_registered(self):
        assert "extract-inline-oneof" in REGISTRY

    def test_apply_unknown_name_raises_with_listing(self):
        with pytest.raises(ValueError, match=r"Unknown post-processor"):
            apply({}, ["does-not-exist"])

    def test_apply_runs_post_processors_in_declared_order(self):
        """Order matters — a contrived A→B test ensures the registry
        respects the user-supplied sequence."""
        record: list[str] = []
        REGISTRY["__test_a"] = lambda s: (record.append("a"), s)[1]
        REGISTRY["__test_b"] = lambda s: (record.append("b"), s)[1]
        try:
            apply({}, ["__test_b", "__test_a"])
            assert record == ["b", "a"]
        finally:
            REGISTRY.pop("__test_a", None)
            REGISTRY.pop("__test_b", None)


class TestExtractInlineOneof:
    """Hoist inline ``oneOf`` + discriminator schemas to named
    components so codegens (openapi-generator's Spring template in
    particular) preserve the polymorphic shape rather than collapsing
    it into a synthetic union DTO."""

    def _spec_with_inline_oneof(self) -> dict:
        return {
            "openapi": "3.0.3",
            "paths": {
                "/datasets/{id}": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "oneOf": [
                                                {"$ref": "#/components/schemas/Dataset"},
                                                {"$ref": "#/components/schemas/Catalog"},
                                                {"$ref": "#/components/schemas/DatasetSeries"},
                                            ],
                                            "discriminator": {
                                                "propertyName": "resourceType",
                                                "mapping": {
                                                    "Dataset": "#/components/schemas/Dataset",
                                                    "Catalog": "#/components/schemas/Catalog",
                                                    "DatasetSeries": "#/components/schemas/DatasetSeries",
                                                },
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {"schemas": {"Dataset": {}, "Catalog": {}, "DatasetSeries": {}}},
        }

    def test_inline_oneof_in_response_becomes_ref(self):
        spec = extract_inline_oneof(self._spec_with_inline_oneof())
        schema = spec["paths"]["/datasets/{id}"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/DatasetVariant"}

    def test_named_component_carries_full_oneof_shape(self):
        spec = extract_inline_oneof(self._spec_with_inline_oneof())
        variant = spec["components"]["schemas"]["DatasetVariant"]
        assert {item["$ref"] for item in variant["oneOf"]} == {
            "#/components/schemas/Dataset",
            "#/components/schemas/Catalog",
            "#/components/schemas/DatasetSeries",
        }
        assert variant["discriminator"]["propertyName"] == "resourceType"

    def test_idempotent(self):
        """Running twice is a no-op — second pass finds no inline
        oneOfs because the first hoisted them all."""
        once = extract_inline_oneof(self._spec_with_inline_oneof())
        twice = extract_inline_oneof(copy.deepcopy(once))
        assert once == twice

    def test_dedupe_identical_inline_shapes_to_single_component(self):
        """The same inline oneOf appearing in multiple operations
        should hoist to a single named component."""
        spec = self._spec_with_inline_oneof()
        # Drop the same shape into a request body too.
        spec["paths"]["/datasets/{id}"]["get"]["requestBody"] = {
            "content": copy.deepcopy(
                spec["paths"]["/datasets/{id}"]["get"]["responses"]["200"]["content"]
            )
        }
        out = extract_inline_oneof(spec)
        # Only one Variant component, both call sites $ref to it.
        variant_keys = [
            k for k in out["components"]["schemas"] if "Variant" in k
        ]
        assert variant_keys == ["DatasetVariant"]

    def test_different_inline_shapes_get_distinct_components(self):
        """Two genuinely different inline oneOfs must not collide on
        a single component — names disambiguate with a numeric tail."""
        spec = self._spec_with_inline_oneof()
        # Swap the request body for a different oneOf (Catalog as seed).
        spec["paths"]["/catalogs/{id}"] = {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {"$ref": "#/components/schemas/Dataset"},
                                        {"$ref": "#/components/schemas/Catalog"},
                                    ],
                                    "discriminator": {
                                        "propertyName": "resourceType",
                                        "mapping": {
                                            "Dataset": "#/components/schemas/Dataset",
                                            "Catalog": "#/components/schemas/Catalog",
                                        },
                                    },
                                }
                            }
                        }
                    }
                }
            }
        }
        out = extract_inline_oneof(spec)
        names = sorted(
            k for k in out["components"]["schemas"] if "Variant" in k
        )
        assert names == ["DatasetVariant", "DatasetVariant2"]

    def test_does_not_touch_inline_oneof_without_discriminator(self):
        """A bare oneOf (no discriminator) is structurally simple; we
        leave it inline so we don't generate noise component schemas."""
        spec = {
            "paths": {
                "/x": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "oneOf": [
                                                {"$ref": "#/components/schemas/A"},
                                                {"$ref": "#/components/schemas/B"},
                                            ]
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {"schemas": {"A": {}, "B": {}}},
        }
        before = copy.deepcopy(spec)
        out = extract_inline_oneof(spec)
        assert (
            out["paths"]["/x"]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
            == before["paths"]["/x"]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
        )

    def test_does_not_recurse_into_existing_component_schemas(self):
        """Component schemas that already host oneOfs (e.g. a
        polymorphic root) are intentionally left alone — they're
        the targets of $refs from path operations, not inline shapes
        themselves. Only path-level inlines are hoisted."""
        spec = {
            "paths": {},
            "components": {
                "schemas": {
                    "Resource": {
                        "oneOf": [
                            {"$ref": "#/components/schemas/A"},
                            {"$ref": "#/components/schemas/B"},
                        ],
                        "discriminator": {"propertyName": "kind"},
                    },
                    "A": {},
                    "B": {},
                }
            },
        }
        before = copy.deepcopy(spec)
        out = extract_inline_oneof(spec)
        assert out["components"]["schemas"]["Resource"] == before["components"][
            "schemas"
        ]["Resource"]


class TestPostProcessorViaGenerator:
    """End-to-end check: the CLI/Generator wiring runs registered
    post-processors against the generated spec."""

    SCHEMA = """
id: https://example.org/poly
name: poly
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
imports:
  - linkml:types
classes:
  Resource:
    abstract: true
    annotations:
      openapi.discriminator: kind
    attributes:
      id:
        identifier: true
        required: true
  Widget:
    is_a: Resource
    annotations:
      openapi.resource: "true"
      openapi.type_value: WIDGET
  Gadget:
    is_a: Resource
    annotations:
      openapi.resource: "true"
      openapi.type_value: GADGET
"""

    def _generate(self, **kwargs) -> dict:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False
        ) as f:
            f.write(self.SCHEMA)
            path = f.name
        try:
            gen = OpenAPIGenerator(path, **kwargs)
            return yaml.safe_load(gen.serialize())
        finally:
            Path(path).unlink(missing_ok=True)

    def test_post_processor_runs_on_dcat3_fixture(self):
        from pathlib import Path

        fixture = str(
            Path(__file__).parent / "fixtures" / "dcat3.yaml"
        )
        canonical = yaml.safe_load(OpenAPIGenerator(fixture).serialize())
        processed = yaml.safe_load(
            OpenAPIGenerator(
                fixture,
                post_processors=["extract-inline-oneof"],
            ).serialize()
        )

        # /datasets/{id} GET 200 went from inline oneOf to $ref.
        canon_schema = canonical["paths"]["/datasets/{id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        proc_schema = processed["paths"]["/datasets/{id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        assert "oneOf" in canon_schema
        assert "$ref" in proc_schema
        # The named component carries the polymorphic shape.
        target_name = proc_schema["$ref"].rsplit("/", 1)[-1]
        target = processed["components"]["schemas"][target_name]
        assert "oneOf" in target
        assert target["discriminator"]["propertyName"] == "resourceType"
