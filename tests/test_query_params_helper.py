"""Unit tests for the shared query-param helper.

Both the OpenAPI generator and the Spring emitter feed schemas through
walk_query_params(); the helper does all parsing, capability inference,
auto-detection, and validation. These tests pin the contract independent
of either renderer.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from linkml_runtime.utils.schemaview import SchemaView

from linkml_openapi._query_params import (
    walk_query_params,
    QueryParamSpec,
    QueryParamSurface,
)


def _sv_from_text(yaml: str, tmp_path: Path) -> SchemaView:
    p = tmp_path / "schema.yaml"
    p.write_text(yaml)
    return SchemaView(str(p))


SCHEMA_BASE = """\
id: https://example.org/qp
name: qp
prefixes:
  linkml: https://w3id.org/linkml/
default_range: string
classes:
  Person:
    annotations:
      openapi.resource: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
      name: { range: string }
      age: { range: integer }
      email: { range: string }
      tags: { range: string, multivalued: true }
"""


def _walk(sv: SchemaView, class_name: str, schema_auto_default: bool = True):
    cls = sv.get_class(class_name)
    return walk_query_params(
        sv,
        cls,
        schema_auto_default=schema_auto_default,
        is_slot_excluded=lambda s: False,
        induced_slots=lambda name: list(sv.class_induced_slots(name)),
        get_slot_annotation=_get_slot_ann,
        get_class_annotation=_get_class_ann,
    )


def _get_slot_ann(cls, slot_name, tag):
    if not cls.slot_usage:
        return None
    su = cls.slot_usage.get(slot_name) if isinstance(cls.slot_usage, dict) else None
    if su is None:
        return None
    anns = getattr(su, "annotations", None)
    if not anns:
        return None
    for ann in anns.values() if isinstance(anns, dict) else [anns]:
        if getattr(ann, "tag", None) == tag:
            return str(ann.value)
    return None


def _get_class_ann(cls, tag):
    if not cls or not cls.annotations:
        return None
    for ann in cls.annotations.values():
        if ann.tag == tag:
            return str(ann.value)
    return None


def test_auto_inference_picks_scalar_slots(tmp_path):
    sv = _sv_from_text(SCHEMA_BASE, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert "name" in names
    assert "age" in names
    assert "email" in names
    assert "tags" not in names    # multivalued auto-excluded
    assert "id" not in names      # identifier auto-excluded
    for spec in surface.params:
        assert spec.capabilities == frozenset({"equality"})
    assert surface.sort_tokens == []


def test_explicit_equality_token(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: equality
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    assert name_spec.capabilities == frozenset({"equality"})


def test_comparable_implies_equality(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      age:
        annotations:
          openapi.query_param: comparable
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    age_spec = next(s for s in surface.params if s.slot.name == "age")
    assert age_spec.capabilities == frozenset({"equality", "comparable"})


def test_sortable_implies_equality_and_emits_sort_tokens(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: sortable
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    assert name_spec.capabilities == frozenset({"equality", "sortable"})
    assert surface.sort_tokens == ["name", "-name"]


def test_query_param_false_excludes_slot_from_auto(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      email:
        annotations:
          openapi.query_param: "false"
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert "email" not in names


def test_auto_query_params_false_at_schema_level_disables_inference(tmp_path):
    sv = _sv_from_text(SCHEMA_BASE, tmp_path)
    surface = _walk(sv, "Person", schema_auto_default=False)
    assert surface.params == []


def test_auto_query_params_class_level_overrides_schema(tmp_path):
    yaml = SCHEMA_BASE.replace(
        '      openapi.resource: "true"',
        '      openapi.resource: "true"\n      openapi.auto_query_params: "true"',
    )
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person", schema_auto_default=False)
    names = [spec.slot.name for spec in surface.params]
    assert "name" in names


def test_unknown_token_warns_and_ignores(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: sorteable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.warns(UserWarning, match="sorteable"):
        surface = _walk(sv, "Person")
    name_spec = next(s for s in surface.params if s.slot.name == "name")
    # auto-inference still applies (typo'd token treated as no annotation)
    assert name_spec.capabilities == frozenset({"equality"})


def test_sortable_on_multivalued_raises(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      tags:
        annotations:
          openapi.query_param: sortable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.raises(ValueError, match="multivalued"):
        _walk(sv, "Person")


def test_comparable_on_string_warns(tmp_path):
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: comparable
"""
    sv = _sv_from_text(yaml, tmp_path)
    with pytest.warns(UserWarning, match="not a numeric or temporal"):
        _walk(sv, "Person")


def test_annotated_class_disables_auto_inference_for_unannotated_slots(tmp_path):
    """When ANY slot on the class has openapi.query_param, auto-inference
    is suppressed for the rest. Matches existing OpenAPI generator behavior."""
    yaml = SCHEMA_BASE + """\
    slot_usage:
      name:
        annotations:
          openapi.query_param: equality
"""
    sv = _sv_from_text(yaml, tmp_path)
    surface = _walk(sv, "Person")
    names = [spec.slot.name for spec in surface.params]
    assert names == ["name"]   # age, email NOT auto-inferred
