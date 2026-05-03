"""Unit tests for the shared chains helper."""
from __future__ import annotations

from pathlib import Path

import pytest
from linkml_runtime.utils.schemaview import SchemaView

from linkml_openapi._chains import (
    build_parent_chains_index,
    canonical_parent_chain,
    parse_path_param_sources,
)

SCHEMA_LINEAR = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      datasets:
        range: Dataset
        multivalued: true
  Dataset:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      distributions:
        range: Distribution
        multivalued: true
  Distribution:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""


def _sv(yaml: str, tmp_path: Path) -> SchemaView:
    p = tmp_path / "schema.yaml"
    p.write_text(yaml)
    return SchemaView(str(p))


def _build_index(sv: SchemaView):
    resource_classes = {
        name for name in sv.all_classes()
        if (sv.get_class(name).annotations or {})
        and any(
            ann.tag == "openapi.resource" and str(ann.value) == "true"
            for ann in sv.get_class(name).annotations.values()
        )
    }
    return build_parent_chains_index(
        sv,
        resource_classes=resource_classes,
        excluded_classes=set(),
        is_slot_excluded=lambda s: False,
        get_slot_annotation=lambda cls, slot, tag: None,
        induced_slots=lambda name: list(sv.class_induced_slots(name)),
    )


def test_chain_index_for_linear_schema(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert index["Distribution"] == [
        [("Catalog", "datasets"), ("Dataset", "distributions")]
    ]
    assert index["Dataset"] == [[("Catalog", "datasets")]]
    assert "Catalog" not in index   # root, no parent chain


def test_canonical_chain_with_one_chain(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert canonical_parent_chain(
        "Distribution", index, parent_path_annotation=None
    ) == [("Catalog", "datasets"), ("Dataset", "distributions")]


def test_canonical_chain_no_parents_returns_empty(tmp_path):
    sv = _sv(SCHEMA_LINEAR, tmp_path)
    index = _build_index(sv)
    assert canonical_parent_chain("Catalog", index, None) == []


def test_canonical_chain_ambiguous_without_annotation_raises(tmp_path):
    yaml = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""
    sv = _sv(yaml, tmp_path)
    index = _build_index(sv)
    with pytest.raises(ValueError, match="multiple parent chains"):
        canonical_parent_chain("Tag", index, None)


def test_canonical_chain_picks_match_via_parent_path(tmp_path):
    yaml = """\
id: https://example.org/c
name: c
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags:
        range: Tag
        multivalued: true
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
"""
    sv = _sv(yaml, tmp_path)
    index = _build_index(sv)
    chain = canonical_parent_chain("Tag", index, "Folder.tags")
    assert chain == [("Folder", "tags")]


def test_parse_path_param_sources_basic():
    out = parse_path_param_sources(
        "ResourceVersion",
        "cId:Catalog.id,doi:ResourceVersion.doi,version:ResourceVersion.version",
    )
    assert out == {
        "cId": ("Catalog", "id"),
        "doi": ("ResourceVersion", "doi"),
        "version": ("ResourceVersion", "version"),
    }


def test_parse_path_param_sources_malformed_raises():
    with pytest.raises(ValueError, match="malformed"):
        parse_path_param_sources("X", "noclassdotpart:notvalidsource")
    with pytest.raises(ValueError, match="malformed"):
        parse_path_param_sources("X", "missingcolon")


def test_parse_path_param_sources_duplicate_name_raises():
    with pytest.raises(ValueError, match="duplicate"):
        parse_path_param_sources("X", "id:Foo.id,id:Bar.id")
