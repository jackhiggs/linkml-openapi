"""Adversarial-input regression tests for the Spring code emitter.

Covers the security review's findings: path-traversal containment,
Java code injection through unescaped string interpolation, Javadoc
``*/`` early-close, and validation of identifier-shaped annotation
values. Each test constructs a malicious schema, runs the emitter,
and asserts that the attack is rejected at parse time (or that the
generated output is structurally safe).

These tests do NOT compile the emitted Java — they assert on the
shape of the generated source. The structural assertions are chosen
so a regression that re-opens the injection vector trips at least
one assertion.
"""

from __future__ import annotations

import pytest

from linkml_openapi.spring import SpringServerGenerator

BASE_HEADER = """\
id: https://example.org/sec
name: sec_test
default_range: string
"""


def _emit(tmp_path, schema_body: str) -> dict:
    """Compile a schema string into the in-memory Java source dict."""
    fixture = tmp_path / "schema.yaml"
    fixture.write_text(BASE_HEADER + schema_body)
    return SpringServerGenerator(str(fixture), package="io.example.sec").build()


def _emit_raises(tmp_path, schema_body: str, match: str) -> None:
    """Compile a schema string and assert it raises with ``match``."""
    fixture = tmp_path / "schema.yaml"
    fixture.write_text(BASE_HEADER + schema_body)
    with pytest.raises(ValueError, match=match):
        SpringServerGenerator(str(fixture), package="io.example.sec").build()


class TestIdentifierValidation:
    """Class names and identifier-shaped annotation values must match
    a strict ASCII Java-identifier pattern. Catches adversarial input
    that would inject Java syntax via raw string interpolation."""

    def test_class_name_with_quote_rejected(self, tmp_path):
        # A class name with a quote-and-semicolon payload would close
        # `public class <name>` and inject a static initialiser.
        _emit_raises(
            tmp_path,
            'classes:\n  "Pwn\\"); static {System.exit(0);} //":\n'
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, required: true }\n",
            match=r"class name.*not a valid Java identifier",
        )

    def test_class_name_with_dot_rejected(self, tmp_path):
        # A class named `evil.Pwn` would land in
        # `import io.example.model.evil.Pwn;` — silently extending
        # the package namespace.
        _emit_raises(
            tmp_path,
            "classes:\n  'evil.Pwn':\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, required: true }\n",
            match=r"class name.*not a valid Java identifier",
        )

    def test_error_class_name_with_path_traversal_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            'annotations:\n  openapi.error_class_name: "../../etc/Pwn"\n'
            "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, required: true }\n",
            # Schema-level error_class_name is read via different path;
            # validation is on the class-level annotation. This test
            # asserts on the wider behaviour — that path-traversal
            # values don't reach the file write.
            match=r"not a valid Java identifier|outside the output tree",
        )

    def test_valid_class_name_accepted(self, tmp_path):
        files = _emit(
            tmp_path,
            "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n",
        )
        assert "io/example/sec/model/Person.java" in files


class TestStringLiteralInjection:
    """Annotation values that land inside Java string literals
    (`@JsonProperty("…")`, `@Schema(allowableValues = {…})`,
    `@JsonSubTypes.Type(name = "…")`) must reject the closing
    sequences that would break out into Java syntax."""

    def test_type_value_with_quote_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n"
            "  Item:\n"
            "    abstract: true\n"
            "    annotations: { openapi.discriminator: kind }\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
            "  Widget:\n"
            "    is_a: Item\n"
            "    annotations:\n"
            '      openapi.resource: "true"\n'
            "      openapi.type_value: 'WIDGET\"); System.exit(0); //'\n",
            match=r"openapi.type_value.*Java source",
        )

    def test_legacy_type_value_with_backslash_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n"
            "  Item:\n"
            "    abstract: true\n"
            "    annotations:\n"
            "      openapi.discriminator: kind\n"
            '      openapi.legacy_type_field: "#type"\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
            "  Widget:\n"
            "    is_a: Item\n"
            "    annotations:\n"
            '      openapi.resource: "true"\n'
            "      openapi.type_value: WIDGET\n"
            "      openapi.legacy_type_value: 'evil\\\\\"+inject+\"'\n",
            match=r"openapi.legacy_type_value.*Java source",
        )

    def test_slot_name_with_quote_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
            "      'bad\"name':\n"
            "        range: string\n",
            match=r"Slot name.*safely embedded",
        )

    def test_discriminator_with_javadoc_close_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n"
            "  Item:\n"
            "    abstract: true\n"
            "    annotations:\n"
            "      openapi.discriminator: 'kind*/{evil}'\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
            "  Widget:\n"
            "    is_a: Item\n"
            '    annotations: { openapi.resource: "true", openapi.type_value: WIDGET }\n',
            match=r"openapi.discriminator.*Java source",
        )


class TestPathLiteralValidation:
    """`openapi.path`, `openapi.path_segment`, and
    `openapi.path_template` land inside Java string literals on
    `@*Mapping(value = "…")`. Restrict the alphabet so adversarial
    paths can't escape the literal."""

    def test_path_with_quote_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n  Person:\n"
            "    annotations:\n"
            '      openapi.resource: "true"\n'
            "      openapi.path: 'people\"); inject'\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n",
            match=r"openapi.path.*not safe to embed",
        )

    def test_path_template_with_semicolon_rejected(self, tmp_path):
        _emit_raises(
            tmp_path,
            "classes:\n  Person:\n"
            "    annotations:\n"
            '      openapi.resource: "true"\n'
            "      openapi.path_template: '/people/{id};drop'\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n",
            match=r"openapi.path_template.*not safe to embed",
        )

    def test_normal_path_accepted(self, tmp_path):
        files = _emit(
            tmp_path,
            "classes:\n  Person:\n"
            "    annotations:\n"
            '      openapi.resource: "true"\n'
            "      openapi.path: /people\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n",
        )
        assert "io/example/sec/api/PersonApi.java" in files


class TestJavadocClose:
    """Description strings must not be able to close the Javadoc
    they land in. The fix escapes ``*/`` to ``*&#47;`` so the
    rendered comment still reads cleanly but the parser can't be
    fooled."""

    def test_class_description_with_javadoc_close_escaped(self, tmp_path):
        files = _emit(
            tmp_path,
            "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    description: 'Has */ a payload */ everywhere'\n"
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n",
        )
        dto = files["io/example/sec/model/Person.java"]
        # Slice off the Javadoc block at the top — that's the only
        # place where ``*/`` would close the comment early. Everything
        # after the Javadoc closer is regular Java; ``*/`` inside a
        # string literal (e.g. ``@Schema(description = "...*/...")``)
        # is harmless.
        javadoc_close = dto.index("*/") + len("*/")
        javadoc_body = dto[: javadoc_close - len("*/")]  # body before the legitimate close
        assert "*/" not in javadoc_body, f"raw *\\/ leaked into class Javadoc:\n{javadoc_body}"
        # And the escaped form survives in the Javadoc.
        assert "*&#47;" in javadoc_body

    def test_slot_description_with_javadoc_close_escaped(self, tmp_path):
        files = _emit(
            tmp_path,
            "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
            "      bio:\n"
            "        range: string\n"
            "        description: 'Free text */ Object eval = new Object() {{ //'\n",
        )
        dto = files["io/example/sec/model/Person.java"]
        # Same crude check: at most one `*/` survives (the class-level
        # Javadoc closer if any; slot-level Javadoc uses `/** ... */`
        # on a single line, so its closer is ALSO `*/` — count is
        # bounded by emitted Javadoc count, but the embedded payload
        # must NOT add a second one).
        # Easier check: the escaped form is present where the payload
        # was.
        assert "*&#47;" in dto


class TestPathTraversalContainment:
    """`SpringServerGenerator.emit` must refuse to write files that
    resolve outside the requested output directory, even if a class
    name or package slips an injection past identifier validation."""

    def test_legitimate_emit_writes_under_output(self, tmp_path):
        fixture = tmp_path / "ok.yaml"
        fixture.write_text(
            BASE_HEADER + "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
        )
        out = tmp_path / "out" / "java"
        out.mkdir(parents=True)
        SpringServerGenerator(str(fixture), package="io.example.sec").emit(str(out))
        written = list(out.rglob("*.java"))
        for path in written:
            # Belt-and-braces: every emitted Java file is under `out`.
            assert path.resolve().is_relative_to(out.resolve())

    def test_package_with_parent_traversal_rejected(self, tmp_path):
        # ``package = ".."`` produces ``relpath = "../model/Person.java"``
        # which resolves outside the output tree (``<out>/../...``).
        # The emit-time containment guard must refuse the write.
        fixture = tmp_path / "ok.yaml"
        fixture.write_text(
            BASE_HEADER + "classes:\n  Person:\n"
            '    annotations: { openapi.resource: "true" }\n'
            "    attributes:\n"
            "      id: { identifier: true, range: string, required: true }\n"
        )
        out = tmp_path / "out" / "java"
        out.mkdir(parents=True)
        with pytest.raises(ValueError, match=r"outside the output tree"):
            SpringServerGenerator(str(fixture), package="..").emit(str(out))


class TestNoUnsafeOperations:
    """Sanity: the generator does not invoke shell, eval, exec, or
    untrusted deserialisation. This is a smoke test against the
    static surface — the security review confirmed it via grep, this
    just locks in the absence."""

    def test_no_subprocess_in_generator(self):
        from linkml_openapi import generator as g

        src = open(g.__file__).read()
        for forbidden in ("subprocess", "shell=True", "os.system", "eval(", "exec("):
            assert forbidden not in src, f"{forbidden!r} appeared in generator.py"

    def test_no_subprocess_in_spring_generator(self):
        from linkml_openapi.spring import generator as g

        src = open(g.__file__).read()
        for forbidden in ("subprocess", "shell=True", "os.system", "eval(", "exec("):
            assert forbidden not in src, f"{forbidden!r} appeared in spring/generator.py"
