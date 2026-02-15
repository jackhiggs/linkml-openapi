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


# --- Spec structure tests ---


class TestSpecStructure:
    def test_openapi_version(self):
        spec = _generate()
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
        assert parsed["openapi"] == "3.1.0"

    def test_json_output(self):
        gen = _make_generator(format="json")
        output = gen.serialize(format="json")
        parsed = json.loads(output)
        assert parsed["openapi"] == "3.1.0"

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
