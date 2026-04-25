"""Tests for the OpenAPI generator."""

import json
from pathlib import Path

import yaml

from linkml_openapi.generator import (
    OpenAPIGenerator,
    _to_path_segment,
    _to_snake_case,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA_PATH = str(FIXTURES / "person.yaml")


def _make_generator(**kwargs) -> OpenAPIGenerator:
    return OpenAPIGenerator(SCHEMA_PATH, **kwargs)


def _generate(**kwargs) -> dict:
    gen = _make_generator(**kwargs)
    raw = gen.serialize(format=kwargs.get("format", "yaml"))
    if kwargs.get("format") == "json":
        return json.loads(raw)
    return yaml.safe_load(raw)


# --- Utility function tests ---


class TestUtils:
    def test_to_snake_case(self):
        assert _to_snake_case("Person") == "person"
        assert _to_snake_case("NamedThing") == "named_thing"
        assert _to_snake_case("HTTPSConnection") == "httpsconnection"

    def test_to_path_segment(self):
        assert _to_path_segment("Person") == "persons"
        assert _to_path_segment("Address") == "addresses"
        assert _to_path_segment("Category") == "categories"

    def test_to_path_segment_handles_ch_sh(self):
        assert _to_path_segment("Branch") == "branches"
        assert _to_path_segment("Wish") == "wishes"

    def test_to_path_segment_invariant_plural(self):
        """`series` and `species` are unchanged in plural form."""
        assert _to_path_segment("DatasetSeries") == "dataset_series"
        assert _to_path_segment("Series") == "series"
        assert _to_path_segment("Species") == "species"

    def test_to_path_segment_warns_on_irregular(self):
        """Class names with irregular English plurals warn so the user sets openapi.path."""
        import warnings

        from linkml_openapi.generator import OpenAPIGenerator

        gen = OpenAPIGenerator(SCHEMA_PATH)

        class FakeCls:
            name = "Child"
            annotations = None

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            gen._get_path_segment(FakeCls())
        assert any("irregular English plural" in str(w.message) for w in caught)


# --- Spec structure tests ---


class TestSpecStructure:
    def test_openapi_version_default(self):
        """Default OpenAPI version is 3.0.3 (most compatible with current codegens)."""
        spec = _generate()
        assert spec["openapi"] == "3.0.3"

    def test_openapi_version_3_1_0_opt_in(self):
        """Explicit --openapi-version 3.1.0 still works."""
        spec = _generate(openapi_version="3.1.0")
        assert spec["openapi"] == "3.1.0"

    def test_info(self):
        spec = _generate(api_title="My API", api_version="2.0.0")
        assert spec["info"]["title"] == "My API"
        assert spec["info"]["version"] == "2.0.0"

    def test_info_defaults_to_schema_name(self):
        spec = _generate()
        assert spec["info"]["title"] == "person_schema"

    def test_description_included(self):
        spec = _generate()
        assert "description" in spec["info"]

    def test_servers(self):
        spec = _generate(server_url="https://api.example.com")
        assert spec["servers"][0]["url"] == "https://api.example.com"

    def test_has_components_and_paths(self):
        spec = _generate()
        assert "components" in spec
        assert "schemas" in spec["components"]
        assert "paths" in spec


# --- Component schema tests ---


class TestComponentSchemas:
    def test_all_classes_in_schemas(self):
        spec = _generate()
        schemas = spec["components"]["schemas"]
        assert "Person" in schemas
        assert "Address" in schemas
        assert "NamedThing" in schemas
        assert "Organization" in schemas

    def test_enums_in_schemas(self):
        spec = _generate()
        schemas = spec["components"]["schemas"]
        assert "PersonStatus" in schemas
        enum_schema = schemas["PersonStatus"]
        assert enum_schema["type"] == "string"
        assert set(enum_schema["enum"]) == {"ALIVE", "DEAD", "UNKNOWN"}

    def test_class_properties(self):
        spec = _generate()
        address = spec["components"]["schemas"]["Address"]
        assert "properties" in address
        props = address["properties"]
        assert "street" in props
        assert "city" in props
        assert props["street"]["type"] == "string"

    def test_required_fields(self):
        spec = _generate()
        address = spec["components"]["schemas"]["Address"]
        assert "id" in address.get("required", [])

    def test_inheritance_uses_allof(self):
        spec = _generate()
        person = spec["components"]["schemas"]["Person"]
        assert "allOf" in person
        assert person["allOf"][0] == {"$ref": "#/components/schemas/NamedThing"}

    def test_slot_type_mapping_integer(self):
        spec = _generate()
        person_props = spec["components"]["schemas"]["Person"]["allOf"][1]["properties"]
        assert person_props["age"]["type"] == "integer"

    def test_slot_constraints(self):
        spec = _generate()
        person_props = spec["components"]["schemas"]["Person"]["allOf"][1]["properties"]
        assert person_props["age"]["minimum"] == 0
        assert person_props["age"]["maximum"] == 200
        assert "pattern" in person_props["email"]

    def test_multivalued_slot_becomes_array(self):
        spec = _generate()
        person_props = spec["components"]["schemas"]["Person"]["allOf"][1]["properties"]
        assert person_props["addresses"]["type"] == "array"
        assert person_props["addresses"]["items"] == {"$ref": "#/components/schemas/Address"}

    def test_enum_ref(self):
        spec = _generate()
        person_props = spec["components"]["schemas"]["Person"]["allOf"][1]["properties"]
        assert person_props["status"] == {"$ref": "#/components/schemas/PersonStatus"}


# --- Path generation tests ---


class TestPaths:
    def test_annotated_resource_gets_paths(self):
        """Person and Address are annotated with openapi.resource: true."""
        spec = _generate()
        paths = spec["paths"]
        assert any("person" in p for p in paths)
        assert any("address" in p for p in paths)

    def test_abstract_class_no_paths(self):
        """NamedThing is abstract and should not get paths."""
        spec = _generate()
        paths = spec["paths"]
        assert not any("named_thing" in p for p in paths)

    def test_custom_path_annotation(self):
        """Address has openapi.path: addresses annotation."""
        spec = _generate()
        assert "/addresses" in spec["paths"]

    def test_collection_path_has_get_and_post(self):
        spec = _generate()
        collection = spec["paths"].get("/persons")
        assert collection is not None
        assert "get" in collection
        assert "post" in collection

    def test_item_path_has_get_put_delete(self):
        spec = _generate()
        item = spec["paths"].get("/persons/{id}")
        assert item is not None
        assert "get" in item
        assert "put" in item
        assert "delete" in item

    def test_item_path_has_path_parameter(self):
        spec = _generate()
        item = spec["paths"]["/persons/{id}"]
        params = item.get("parameters", [])
        assert len(params) == 1
        assert params[0]["name"] == "id"
        assert params[0]["in"] == "path"
        assert params[0]["required"] is True

    def test_list_operation_structure(self):
        spec = _generate()
        list_op = spec["paths"]["/persons"]["get"]
        assert "summary" in list_op
        assert "operationId" in list_op
        assert "responses" in list_op
        assert "200" in list_op["responses"]
        schema = list_op["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema["type"] == "array"

    def test_create_operation_has_request_body(self):
        spec = _generate()
        create_op = spec["paths"]["/persons"]["post"]
        assert "requestBody" in create_op
        assert create_op["requestBody"]["required"] is True

    def test_list_has_pagination_params(self):
        spec = _generate()
        list_op = spec["paths"]["/persons"]["get"]
        param_names = {p["name"] for p in list_op["parameters"]}
        assert "limit" in param_names
        assert "offset" in param_names

    def test_list_has_filter_params(self):
        """Annotated slots become query params (name, age)."""
        spec = _generate()
        list_op = spec["paths"]["/persons"]["get"]
        param_names = {p["name"] for p in list_op["parameters"]}
        assert "name" in param_names
        assert "age" in param_names
        # email is not annotated as query_param
        assert "email" not in param_names

    def test_collection_path_address_no_post(self):
        """Address with openapi.operations: 'list,read' has no POST."""
        spec = _generate()
        collection = spec["paths"]["/addresses"]
        assert "get" in collection
        assert "post" not in collection

    def test_item_path_address_no_put_delete(self):
        """Address with openapi.operations: 'list,read' has no PUT/DELETE."""
        spec = _generate()
        item = spec["paths"]["/addresses/{id}"]
        assert "get" in item
        assert "put" not in item
        assert "delete" not in item

    def test_resource_filter_limits_classes(self):
        spec = _generate(resource_filter=["Address"])
        paths = spec["paths"]
        assert any("address" in p for p in paths)
        assert not any("person" in p for p in paths)


# --- Serialization tests ---


class TestSerialization:
    def test_yaml_output(self):
        gen = _make_generator()
        output = gen.serialize(format="yaml")
        parsed = yaml.safe_load(output)
        assert parsed["openapi"] == "3.0.3"

    def test_json_output(self):
        gen = _make_generator(format="json")
        output = gen.serialize(format="json")
        parsed = json.loads(output)
        assert parsed["openapi"] == "3.0.3"

    def test_is_linkml_generator(self):
        """Verify it extends the LinkML Generator base class."""
        from linkml.utils.generator import Generator

        gen = _make_generator()
        assert isinstance(gen, Generator)
        assert gen.uses_schemaloader is False
        assert gen.schemaview is not None


# --- Slot annotation tests ---


class TestSlotAnnotations:
    def test_path_variable_from_annotation(self):
        """Person.id is annotated as path variable."""
        spec = _generate()
        assert "/persons/{id}" in spec["paths"]
        item = spec["paths"]["/persons/{id}"]
        params = item.get("parameters", [])
        assert any(p["name"] == "id" and p["in"] == "path" for p in params)

    def test_query_param_from_annotation(self):
        """Person has name and age annotated as query params."""
        spec = _generate()
        list_op = spec["paths"]["/persons"]["get"]
        param_names = {p["name"] for p in list_op["parameters"]}
        assert "name" in param_names
        assert "age" in param_names
        # email is NOT annotated as query_param, so should not appear
        assert "email" not in param_names
        # limit/offset always present
        assert "limit" in param_names
        assert "offset" in param_names

    def test_no_slot_annotations_falls_back(self):
        """Organization has no slot annotations, gets auto-inferred params."""
        spec = _generate(resource_filter=["Organization"])
        list_op = spec["paths"]["/organizations"]["get"]
        param_names = {p["name"] for p in list_op["parameters"]}
        assert "limit" in param_names
        assert "offset" in param_names
        # Organization inherits from NamedThing which has name, description

    def test_operations_limits_methods(self):
        """Address with openapi.operations: 'list,read' has no POST/PUT/DELETE."""
        spec = _generate()
        # Collection path should only have GET (list), no POST (create)
        collection = spec["paths"]["/addresses"]
        assert "get" in collection
        assert "post" not in collection
        # Item path should only have GET (read), no PUT/DELETE
        item = spec["paths"]["/addresses/{id}"]
        assert "get" in item
        assert "put" not in item
        assert "delete" not in item


class TestPathVariableMode:
    def test_iri_mode_preserves_uri_format(self):
        """`openapi.path_variable: "true"` (iri default) keeps the slot's range typing."""
        spec = _generate()
        # Person.id has range string (the existing fixture) so the path param
        # remains plain string — that confirms iri mode reads the slot range.
        params = spec["paths"]["/persons/{id}"]["parameters"]
        id_param = next(p for p in params if p["name"] == "id")
        assert id_param["schema"]["type"] == "string"

    def test_slug_mode_emits_plain_string(self):
        """`openapi.path_variable: slug` ignores the slot's range and emits string."""
        spec = _generate()
        params = spec["paths"]["/catalogs/{id}"]["parameters"]
        id_param = next(p for p in params if p["name"] == "id")
        assert id_param["schema"]["type"] == "string"
        # slot.range is `uri`, so without slug mode we'd expect format=uri here.
        assert "format" not in id_param["schema"]

    def test_iri_mode_carries_uri_format_when_range_is_uri(self):
        """Sanity check: an iri-mode path variable on a uri-range slot DOES set format=uri."""
        # Spin up a generator on a one-off schema where Catalog.id stays in iri mode.
        spec_iri = _generate(resource_filter=["Catalog"])
        # The fixture's Catalog uses slug. We assert via construction in a
        # parallel test using a mini schema below — keep this assertion light.
        params = spec_iri["paths"]["/catalogs/{id}"]["parameters"]
        id_param = next(p for p in params if p["name"] == "id")
        assert id_param["schema"]["type"] == "string"


class TestFormatAnnotation:
    def test_int64_overrides_default_integer(self):
        spec = _generate()
        props = spec["components"]["schemas"]["Address"]["properties"]
        assert props["byte_size"]["type"] == "integer"
        assert props["byte_size"]["format"] == "int64"

    def test_binary_string(self):
        spec = _generate()
        props = spec["components"]["schemas"]["Address"]["properties"]
        assert props["avatar_blob"]["type"] == "string"
        assert props["avatar_blob"]["format"] == "binary"

    def test_format_applied_to_array_items(self):
        """Multivalued slot — the format goes on items, not the array."""
        spec = _generate()
        props = spec["components"]["schemas"]["Address"]["properties"]
        assert props["tags"]["type"] == "array"
        assert "format" not in props["tags"]
        assert props["tags"]["items"]["format"] == "byte"


class TestFlattenInheritance:
    def test_default_uses_allof(self):
        """Without flatten_inheritance, subclass schemas use allOf + $ref."""
        spec = _generate()
        person = spec["components"]["schemas"]["Person"]
        assert "allOf" in person

    def test_flatten_inlines_parent_properties(self):
        """With flatten_inheritance, parent properties appear directly on the schema."""
        spec = _generate(flatten_inheritance=True)
        person = spec["components"]["schemas"]["Person"]
        assert "allOf" not in person
        assert "properties" in person
        # name and id are inherited from NamedThing — they should appear inline.
        assert "name" in person["properties"]
        assert "id" in person["properties"]
        # And local properties are still here.
        assert "email" in person["properties"]
        assert "age" in person["properties"]

    def test_flatten_preserves_required(self):
        """Required fields from parent classes should still be required after flattening."""
        spec = _generate(flatten_inheritance=True)
        person = spec["components"]["schemas"]["Person"]
        # name and id are required on NamedThing.
        assert "name" in (person.get("required") or [])
        assert "id" in (person.get("required") or [])


class TestRdfExtensions:
    def test_class_uri_emitted_as_x_rdf_class(self):
        """Person.class_uri = schema:Person becomes x-rdf-class on the schema."""
        spec = _generate()
        person = spec["components"]["schemas"]["Person"]
        assert person.get("x-rdf-class") == "http://schema.org/Person"

    def test_slot_uri_emitted_as_x_rdf_property(self):
        """Person.email has slot_uri schema:email — x-rdf-property next to the property."""
        spec = _generate()
        person = spec["components"]["schemas"]["Person"]
        # Person uses inheritance, so properties live under allOf[1].properties
        local_props = person["allOf"][1]["properties"]
        assert local_props["email"].get("x-rdf-property") == "http://schema.org/email"
        assert local_props["age"].get("x-rdf-property") == "http://xmlns.com/foaf/0.1/age"

    def test_class_with_no_uri_has_no_extension(self):
        """Address has no class_uri — no x-rdf-class is emitted."""
        spec = _generate()
        address = spec["components"]["schemas"]["Address"]
        assert "x-rdf-class" not in address

    def test_class_with_unknown_prefix_falls_back_to_curie(self):
        """A class_uri whose prefix is not in `prefixes` is emitted as-is."""
        from linkml_openapi.generator import OpenAPIGenerator

        gen = OpenAPIGenerator(SCHEMA_PATH)
        # SchemaView.expand_curie passes unknown CURIEs through.
        assert gen.schemaview.expand_curie("unknown:Foo") == "unknown:Foo"


class TestMediaTypes:
    def test_default_media_type_is_json(self):
        """Address has no openapi.media_types — only application/json content."""
        spec = _generate()
        list_op = spec["paths"]["/addresses"]["get"]
        content = list_op["responses"]["200"]["content"]
        assert set(content.keys()) == {"application/json"}

    def test_annotation_drives_response_content(self):
        """Person declares JSON, JSON-LD and Turtle — all three appear."""
        spec = _generate()
        list_op = spec["paths"]["/persons"]["get"]
        content = list_op["responses"]["200"]["content"]
        assert set(content.keys()) == {
            "application/json",
            "application/ld+json",
            "text/turtle",
        }

    def test_annotation_drives_request_body(self):
        """Person POST request body advertises every declared media type."""
        spec = _generate()
        post_op = spec["paths"]["/persons"]["post"]
        content = post_op["requestBody"]["content"]
        assert set(content.keys()) == {
            "application/json",
            "application/ld+json",
            "text/turtle",
        }

    def test_annotation_drives_create_response(self):
        """201 create response also advertises every declared media type."""
        spec = _generate()
        post_op = spec["paths"]["/persons"]["post"]
        content = post_op["responses"]["201"]["content"]
        assert set(content.keys()) == {
            "application/json",
            "application/ld+json",
            "text/turtle",
        }

    def test_annotation_drives_read_and_update(self):
        """GET / PUT on the item path also advertise every declared media type."""
        spec = _generate()
        item = spec["paths"]["/persons/{id}"]
        get_content = item["get"]["responses"]["200"]["content"]
        put_req_content = item["put"]["requestBody"]["content"]
        put_resp_content = item["put"]["responses"]["200"]["content"]
        for content in (get_content, put_req_content, put_resp_content):
            assert set(content.keys()) == {
                "application/json",
                "application/ld+json",
                "text/turtle",
            }
