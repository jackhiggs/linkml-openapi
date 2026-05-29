"""Tests for the test helpers themselves.

``_generate_from_string`` / ``_generate_from_string_raises`` carry
nontrivial logic (temp-file write, conditional cleanup, format
toggle, exception propagation) and are used by ~100 schema-based
tests. A silent regression in either would mask failures across
the entire suite — these tests pin the helpers' contract so a
regression trips here first.
"""

from __future__ import annotations

import os

import pytest

from tests.test_generator import _generate_from_string, _generate_from_string_raises

MINIMAL_SCHEMA = """\
id: https://example.org/helpers
name: helpers
default_range: string
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id: { identifier: true, range: string, required: true }
"""


class TestGenerateFromString:
    """`_generate_from_string` round-trips a YAML schema through the
    OpenAPI generator and returns the parsed dict."""

    def test_returns_spec_dict(self):
        spec = _generate_from_string(MINIMAL_SCHEMA)
        assert isinstance(spec, dict)
        assert spec["openapi"].startswith("3.")
        assert "/people" in spec["paths"]

    def test_accepts_kwargs(self):
        spec = _generate_from_string(MINIMAL_SCHEMA, openapi_version="3.1.0")
        assert spec["openapi"] == "3.1.0"

    def test_cleans_up_temp_file(self, monkeypatch, tmp_path):
        # Intercept the tempfile path so we can assert it was unlinked.
        captured: dict[str, str] = {}
        from tests import test_generator as tg

        original_factory = tg.tempfile.NamedTemporaryFile

        def _capturing_factory(*args, **kwargs):
            f = original_factory(*args, **kwargs)
            captured["path"] = f.name
            return f

        monkeypatch.setattr(tg.tempfile, "NamedTemporaryFile", _capturing_factory)
        _generate_from_string(MINIMAL_SCHEMA)
        assert "path" in captured, "tempfile factory was not called"
        assert not os.path.exists(captured["path"]), f"helper leaked temp file {captured['path']}"


class TestGenerateFromStringRaises:
    """`_generate_from_string_raises` asserts that the generator
    raises with the configured regex match. Verifies the assertion
    propagates, the message pattern is honoured, and successful runs
    fail the test (since the helper expects a raise)."""

    # `openapi.list_query_params` with malformed JSON is a small,
    # reliable error path that exercises the helper without depending
    # on the heavier polymorphism setup.
    BROKEN_SCHEMA = """\
id: https://example.org/broken
name: broken
default_range: string
classes:
  Person:
    annotations:
      openapi.resource: "true"
      openapi.path: people
      openapi.list_query_params: "not valid json"
    attributes:
      id: { identifier: true, range: string, required: true }
"""

    def test_propagates_value_error_with_match(self):
        _generate_from_string_raises(self.BROKEN_SCHEMA, match=r"JSON array")

    def test_fails_when_schema_does_not_raise(self):
        # The helper itself uses pytest.raises, which raises
        # ``_pytest.outcomes.Failed`` (a BaseException subclass) when
        # the body completes without an exception. Catch with
        # BaseException so the assertion is robust to pytest
        # internals.
        with pytest.raises(BaseException, match=r"DID NOT RAISE"):
            _generate_from_string_raises(MINIMAL_SCHEMA, match=r"never matches")

    def test_match_regex_is_honoured(self):
        # A mismatching pattern fails the test even though the schema
        # does raise — proves the match argument is enforced.
        with pytest.raises(AssertionError):
            _generate_from_string_raises(self.BROKEN_SCHEMA, match=r"completely unrelated wording")


class TestPersonSpecFixtureCaching:
    """The module-level ``_PERSON_SPEC_CACHE`` in test_generator caches
    no-kwarg ``_generate()`` calls. Tests below verify the caching is
    invisible to consumers — the cached dict is identity-equal across
    calls, and any kwarg bypasses the cache (a fresh build runs)."""

    def test_no_kwarg_returns_cached_object(self):
        from tests.test_generator import _generate

        first = _generate()
        second = _generate()
        assert first is second, "no-kwarg _generate should return cached dict"

    def test_kwarg_bypasses_cache(self):
        from tests.test_generator import _generate

        a = _generate()
        b = _generate(api_version="2.0.0")
        # Different result (different api_version) → not the cached dict.
        assert b is not a
        assert b["info"]["version"] == "2.0.0"
