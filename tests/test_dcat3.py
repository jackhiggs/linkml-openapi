"""DCAT-3 (W3C, https://www.w3.org/TR/vocab-dcat-3/) generation tests.

Pins the OpenAPI shape emitted from the DCAT-3 core fixture against the
behaviours that openapi-generator's Spring template (and other Java/TS
codegens) need:

  * Multi-level ``is_a`` chains stay chained.  Resource → Dataset → Catalog
    must emit three separate ``allOf: [{$ref: <parent>}, {properties}]``
    schemas, not a flattened single layer.
  * The discriminator hoists once.  ``openapi.discriminator: resourceType``
    on Resource produces a ``discriminator`` + ``oneOf`` block on the
    Resource schema only; subclasses inherit it via the ``$ref``.
  * Polymorphic slot ranges resolve through the discriminator-bearing
    schema.  ``CatalogRecord.primaryTopic: Resource`` is a bare ``$ref``
    to Resource — Spring codegen relies on this so its generated DTO has
    a single Resource interface, not a duplicated oneOf.
  * Recursive slot ranges become ``$ref`` loops.  ``Catalog.catalog`` →
    ``$ref Catalog`` (no inlining, no infinite expansion).
  * ``class_uri`` lands on every emitted schema as ``x-rdf-class``,
    preserving DCAT-3 RDF identity even though the field name on the
    wire is ``resourceType`` (not ``@type``).
  * Abstract classes do not emit resource paths.
"""

from pathlib import Path

import pytest
import yaml

from linkml_openapi.generator import OpenAPIGenerator

FIXTURE = str(Path(__file__).parent / "fixtures" / "dcat3.yaml")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(OpenAPIGenerator(FIXTURE).serialize())


@pytest.fixture(scope="module")
def schemas(spec) -> dict:
    return spec["components"]["schemas"]


def _props(schema: dict) -> dict:
    """Return a class schema's properties regardless of whether it uses
    ``allOf`` (inherited form, ``allOf[1]['properties']``) or is flat
    (top-level ``properties``).

    Polymorphic class chains use ``allOf`` so codegens like
    openapi-generator's Spring template produce idiomatic Java
    inheritance — Catalog extends Dataset extends Resource — and the
    spec round-trips through Spring + springdoc with full polymorphic
    dispatch preserved. Tests use this helper so they don't break when
    ``flatten_inheritance`` is toggled.
    """
    if "allOf" in schema:
        for part in schema["allOf"]:
            if isinstance(part, dict) and "properties" in part:
                return part["properties"]
        return {}
    return schema.get("properties") or {}


def _required(schema: dict) -> list:
    """Return a class schema's required list, regardless of allOf shape."""
    if "allOf" in schema:
        for part in schema["allOf"]:
            if isinstance(part, dict) and "required" in part:
                return part["required"]
        return []
    return schema.get("required") or []


class TestDiscriminatorPlacement:
    """Polymorphism lives at points-of-use, not at the schema level.

    A schema-level ``discriminator`` + ``oneOf`` on the polymorphic root
    bleeds into every subclass via ``allOf: [{$ref: <root>}]`` — Swagger
    UI and several codegens render it as if every subclass also offers
    the polymorphic union. The cleanest fix is to express polymorphism
    *only* at the join points: slot ranges (when composition reaches a
    polymorphic class) and path responses. Both are already done by
    :meth:`_class_range_ref` and :meth:`_class_response_ref`. The
    discriminator field itself still lives on the root schema as a
    plain property (``type: string, enum: […]``) so the wire payload
    is unambiguous.
    """

    def test_resource_has_no_schema_level_discriminator(self, schemas):
        assert "discriminator" not in schemas["Resource"]

    def test_resource_has_no_schema_level_oneof(self, schemas):
        assert "oneOf" not in schemas["Resource"]

    def test_resource_does_not_carry_discriminator_field_itself(self, schemas):
        """An abstract polymorphic root has no instances on the wire, so it
        doesn't need ``resourceType`` in its own properties either. Each
        concrete subclass declares its own pinned ``resourceType`` in
        its local block via ``_inject_subclass_type_value``."""
        assert "resourceType" not in _props(schemas["Resource"])
        assert "resourceType" not in _required(schemas["Resource"])

    @pytest.mark.parametrize("subclass", ["Dataset", "Catalog", "DatasetSeries", "DataService"])
    def test_subclass_has_no_schema_level_polymorphism(self, schemas, subclass):
        """Subclass schemas stay clean: no inherited discriminator+oneOf
        bleeding through allOf — the subclass's display in Swagger UI
        shows just its own and inherited properties, no sibling list."""
        assert "discriminator" not in schemas[subclass]
        assert "oneOf" not in schemas[subclass]

    @pytest.mark.parametrize(
        "concrete",
        ["Dataset", "Catalog", "DatasetSeries", "DataService"],
    )
    def test_every_concrete_class_pins_resource_type_to_self(self, schemas, concrete):
        """Each concrete class declares its own ``resourceType`` pinned
        to ``enum: [<self>]`` with a matching ``default`` in its local
        block. Under the ``allOf``-based inheritance shape, the parent
        Dataset's pin and Catalog's pin theoretically intersect to ∅
        for JSON Schema purists — but openapi-generator's Spring
        template doesn't run JSON Schema validation; it dispatches via
        the discriminator at slot/path joins and Jackson polymorphism,
        which preserves the per-class identity at the codegen layer."""
        prop = _props(schemas[concrete])["resourceType"]
        assert prop["enum"] == [concrete]
        assert prop["default"] == concrete
        assert "resourceType" in _required(schemas[concrete])

    def test_resource_type_carries_no_field_level_rdf_property(self, schemas):
        """The discriminator's RDF identity belongs to the class
        (``x-rdf-class`` + ``x-jsonld-type``), not to the field — so
        no field-level ``x-rdf-property`` is emitted, otherwise an
        RDF runtime would synthesise a malformed
        ``<subject> rdf:type "Dataset"`` literal triple."""
        for cls in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            prop = _props(schemas[cls])["resourceType"]
            assert "x-rdf-property" not in prop

    @pytest.mark.parametrize(
        "concrete,legacy_value",
        [
            ("Dataset", "com.xyz.dcat.Dataset"),
            ("Catalog", "com.xyz.dcat.Catalog"),
            ("DatasetSeries", "com.xyz.dcat.DatasetSeries"),
            ("DataService", "com.xyz.dcat.DataService"),
        ],
    )
    def test_legacy_type_field_pins_per_class_constant(self, schemas, concrete, legacy_value):
        """``openapi.legacy_type_field: "#type"`` on the polymorphic root
        synthesises a back-compat marker on every concrete class. Each
        class's ``openapi.legacy_type_value`` becomes the pinned constant
        — values can be opaque (Java FQN, custom IRI, anything stable)
        because the field is for back-compat, not RDF semantics."""
        prop = _props(schemas[concrete])["#type"]
        assert prop["enum"] == [legacy_value]
        assert prop["default"] == legacy_value
        assert prop["type"] == "string"
        assert "#type" in _required(schemas[concrete])

    def test_legacy_type_field_carries_no_rdf_property(self, schemas):
        """The legacy field is a back-compat convention, not an RDF
        predicate — no ``x-rdf-property`` should be attached to it."""
        for cls in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            assert "x-rdf-property" not in _props(schemas[cls])["#type"]

    def test_legacy_type_field_carries_no_x_codegen_name_extension(self, schemas):
        """openapi-generator does not have a universal property-renaming
        vendor extension, so we deliberately do not emit one. The rename
        is captured separately via :meth:`emit_name_mappings` and lands
        in a sibling file the user passes to
        ``openapi-generator --name-mappings @<file>``."""
        for cls in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            assert "x-codegen-name" not in _props(schemas[cls])["#type"]

    def test_distribution_does_not_carry_legacy_type_field(self, schemas):
        """Distribution is outside the polymorphic chain, so neither
        the proper discriminator nor the legacy field appears on it."""
        props = _props(schemas["Distribution"])
        assert "#type" not in props
        assert "resourceType" not in props

    def test_distribution_does_not_carry_discriminator(self, schemas):
        """Distribution is outside the Resource hierarchy. The discriminator
        must not leak across hierarchies."""
        assert "discriminator" not in schemas["Distribution"]
        assert "oneOf" not in schemas["Distribution"]
        assert "resourceType" not in _props(schemas["Distribution"])


class TestPolymorphicInheritance:
    """Polymorphic class chains use ``allOf: [{$ref: parent}, {local}]``
    so codegens (notably openapi-generator's Spring template) produce
    idiomatic ``Catalog extends Dataset extends Resource`` Java
    inheritance with proper Jackson polymorphic dispatch — and the
    spec round-trips through Spring + springdoc with discriminator
    handling preserved. Use ``flatten_inheritance`` to inline if a
    consumer prefers self-contained schemas.
    """

    @pytest.mark.parametrize(
        "concrete,parent",
        [
            ("Dataset", "Resource"),
            ("Catalog", "Dataset"),
            ("DatasetSeries", "Dataset"),
            ("DataService", "Resource"),
        ],
    )
    def test_polymorphic_class_extends_parent_via_allof(self, schemas, concrete, parent):
        """Each concrete polymorphic class chains to its immediate
        ancestor via ``allOf[0]`` — the chain isn't collapsed to the
        root Resource. Codegen produces matching Java inheritance."""
        sch = schemas[concrete]
        assert "allOf" in sch
        assert sch["allOf"][0] == {"$ref": f"#/components/schemas/{parent}"}

    def test_resource_remains_concrete_object_for_codegen(self, schemas):
        """``Resource`` is abstract in LinkML, but emitted as
        ``type: object`` with its own properties so codegen produces a
        Resource base class that subclasses extend."""
        sch = schemas["Resource"]
        assert sch["type"] == "object"
        assert "id" in (sch.get("properties") or {})


class TestComposition:
    """Composition vs reference distinguishes ownership.

    * ``Dataset.distribution`` (``inlined: true``) — distributions are
      part of the parent dataset payload.
    * ``Location`` and ``PeriodOfTime`` have no identifier slot, so
      LinkML defaults their ranges to composition; ``Dataset.spatial``
      and ``Dataset.temporal`` embed structured value objects.
    * Everything else (``publisher``, ``creator``, ``catalog``,
      ``inSeries``, …) is a reference IRI — LinkML's default for
      identifier-bearing target classes.
    """

    def test_dataset_distribution_items_embed_distribution_schema(self, schemas):
        prop = _props(schemas["Dataset"])["distribution"]
        assert prop["type"] == "array"
        # Distribution has no concrete descendants, so items are a plain
        # $ref — not a oneOf wrapper.
        assert prop["items"] == {"$ref": "#/components/schemas/Distribution"}

    def test_nested_distribution_collection_path_emitted(self, spec):
        """Composition produces nested CRUD paths under the parent."""
        assert "/datasets/{id}/distribution" in spec["paths"]
        assert "/datasets/{id}/distribution/{distribution_id}" in spec["paths"]

    @staticmethod
    def _ref_target(prop: dict) -> str | None:
        """Resolve the target of a property whether emitted as a bare
        ``$ref`` or wrapped in ``allOf: [{$ref: …}]`` (the wrapper form
        is used when the slot also carries a description / pattern /
        bounds — the wrapper lets those sit alongside the reference)."""
        if "$ref" in prop:
            return prop["$ref"]
        if "allOf" in prop:
            for part in prop["allOf"]:
                if isinstance(part, dict) and "$ref" in part:
                    return part["$ref"]
        return None

    def test_dataset_spatial_embeds_location_object(self, schemas):
        """``Location`` has no identifier slot, so the LinkML default
        is composition — the spatial value travels embedded in the
        dataset payload, not as an IRI."""
        prop = _props(schemas["Dataset"])["spatial"]
        assert self._ref_target(prop) == "#/components/schemas/Location"

    def test_dataset_temporal_embeds_period_object(self, schemas):
        prop = _props(schemas["Dataset"])["temporal"]
        assert self._ref_target(prop) == "#/components/schemas/PeriodOfTime"

    def test_distribution_checksum_embeds_checksum_object(self, schemas):
        """SPDX-style checksum is structured but not identified — composed."""
        prop = _props(schemas["Distribution"])["checksum"]
        assert self._ref_target(prop) == "#/components/schemas/Checksum"

    @pytest.mark.parametrize(
        "embedded_class,property_name,property_type",
        [
            ("Location", "geometry", "string"),
            ("Location", "bbox", "string"),
            ("PeriodOfTime", "startDate", "string"),
            ("PeriodOfTime", "endDate", "string"),
            ("Checksum", "checksumValue", "string"),
        ],
    )
    def test_embedded_value_class_has_no_identifier(
        self, schemas, embedded_class, property_name, property_type
    ):
        """Embedded classes deliberately omit any identifier so LinkML
        defaults them to composition. Spec-side: no ``id`` in
        properties, no ``id`` in required."""
        sch = schemas[embedded_class]
        assert "id" not in (sch.get("properties") or {})
        assert "id" not in (sch.get("required") or [])
        assert property_name in sch["properties"]


class TestAgentClass:
    """``Agent`` is a top-level resource (``foaf:Agent``) with its own
    CRUD endpoints. Resource-level slots (``publisher``, ``creator``,
    ``contributor``, ``contactPoint``) reference Agent by IRI, not
    embedded — the LinkML default for identifier-bearing target
    classes."""

    @staticmethod
    def _is_iri_string(prop: dict) -> bool:
        return prop.get("type") == "string" and prop.get("format") == "uri"

    def test_agent_endpoints_emitted(self, spec):
        assert "/agents" in spec["paths"]
        assert "/agents/{id}" in spec["paths"]

    def test_agent_schema_carries_foaf_class_uri(self, schemas):
        assert schemas["Agent"]["x-rdf-class"] == "http://xmlns.com/foaf/0.1/Agent"

    @pytest.mark.parametrize("agent_slot", ["publisher", "creator", "contributor", "contactPoint"])
    def test_dataset_agent_slots_are_iri_references(self, schemas, agent_slot):
        # Dataset inherits these from Resource via allOf, so they live
        # on Resource's local schema, not Dataset's. Use a chain walk.
        prop = _props(schemas["Resource"])[agent_slot]
        if prop.get("type") == "array":
            assert self._is_iri_string(prop["items"])
        else:
            assert self._is_iri_string(prop)

    def test_agent_inherited_into_every_polymorphic_class(self, schemas):
        """Resource defines the agent slots → Dataset, Catalog, etc.
        inherit them via ``allOf [{$ref: Resource}, ...]``. The
        properties live on Resource itself; subclass DTOs see them
        through the chain."""
        resource_props = _props(schemas["Resource"])
        assert "publisher" in resource_props
        assert "creator" in resource_props
        for cls in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            assert schemas[cls]["allOf"][0]["$ref"].startswith("#/components/schemas/")


class TestVersioning:
    """DCAT-3 versioning slots all reference Resource (the polymorphic
    root) via IRI — they don't embed full payloads."""

    @staticmethod
    def _is_iri_string(prop: dict) -> bool:
        return prop.get("type") == "string" and prop.get("format") == "uri"

    @pytest.mark.parametrize(
        "version_slot,multivalued",
        [
            ("isVersionOf", False),
            ("hasVersion", True),
            ("hasCurrentVersion", False),
            ("previousVersion", False),
            ("replaces", False),
            ("isReplacedBy", False),
        ],
    )
    def test_versioning_slot_is_iri_reference(self, schemas, version_slot, multivalued):
        # Defined on Resource → inherited via allOf into Dataset etc.
        prop = _props(schemas["Resource"])[version_slot]
        if multivalued:
            assert prop["type"] == "array"
            assert self._is_iri_string(prop["items"])
        else:
            assert self._is_iri_string(prop)

    def test_version_string_slots_are_plain_strings(self, schemas):
        """``version`` and ``versionNotes`` are literal strings (adms:),
        not URIs."""
        version = _props(schemas["Resource"])["version"]
        assert version["type"] == "string"
        assert "format" not in version


class TestReferenceSlots:
    """Class-ranged slots default to ``inlined: false`` — they emit a
    plain IRI string (``type: string, format: uri``), not a full
    embedded copy.

    DCAT-3 is RDF-first: every slot above is a *relation*, not
    composition. The wire JSON is ``{"dataset": ["https://…/d/1"]}``,
    never an embedded Dataset payload. The same shape stays valid when
    a content-negotiated ``application/ld+json`` or ``text/turtle``
    response is rendered from the same data, because in RDF a relation
    is always an IRI — wrapping it in ``{id: …}`` would be invalid.
    """

    URI = {"type": "string", "format": "uri"}

    def test_catalog_record_primary_topic_is_iri_string(self, schemas):
        prop = _props(schemas["CatalogRecord"])["primaryTopic"]
        assert prop["type"] == "string"
        assert prop["format"] == "uri"

    def test_catalog_dataset_array_items_are_iri_strings(self, schemas):
        prop = _props(schemas["Catalog"])["dataset"]
        assert prop["type"] == "array"
        assert prop["items"] == self.URI

    def test_data_service_serves_dataset_array_items_are_iri_strings(self, schemas):
        prop = _props(schemas["DataService"])["servesDataset"]
        assert prop["type"] == "array"
        assert prop["items"] == self.URI


class TestRecursionDissolves:
    """When ``Catalog.catalog`` is treated as a relation (the LinkML default
    for identifier-bearing target classes), the self-cycle in the spec
    disappears: the items are IRI strings, not embedded ``Catalog``
    payloads. Swagger UI no longer renders an infinite expansion."""

    URI = {"type": "string", "format": "uri"}

    def test_catalog_self_reference_does_not_recurse(self, schemas):
        prop = _props(schemas["Catalog"])["catalog"]
        assert prop["type"] == "array"
        assert prop["items"] == self.URI

    def test_dataset_in_series_is_iri_string(self, schemas):
        """The W3C DCAT-3 link goes from a Dataset to its DatasetSeries
        via ``dcat:inSeries`` — there is no inverse ``seriesMember``
        slot on the series. The dataset side carries the IRI reference;
        the series's members are recovered by querying datasets whose
        ``inSeries`` includes the series IRI."""
        prop = _props(schemas["Dataset"])["inSeries"]
        assert prop["type"] == "array"
        assert prop["items"] == self.URI
        # No forward seriesMember on DatasetSeries.
        assert "seriesMember" not in _props(schemas["DatasetSeries"])


class TestRdfIdentity:
    """`class_uri: dcat:*` survives the round-trip as `x-rdf-class`."""

    @pytest.mark.parametrize(
        "class_name,curie",
        [
            ("Resource", "http://www.w3.org/ns/dcat#Resource"),
            ("Dataset", "http://www.w3.org/ns/dcat#Dataset"),
            ("Catalog", "http://www.w3.org/ns/dcat#Catalog"),
            ("DatasetSeries", "http://www.w3.org/ns/dcat#DatasetSeries"),
            ("DataService", "http://www.w3.org/ns/dcat#DataService"),
            ("Distribution", "http://www.w3.org/ns/dcat#Distribution"),
            ("CatalogRecord", "http://www.w3.org/ns/dcat#CatalogRecord"),
        ],
    )
    def test_x_rdf_class_matches_expanded_curie(self, schemas, class_name, curie):
        assert schemas[class_name]["x-rdf-class"] == curie

    def test_slot_uri_lands_as_x_rdf_property(self, schemas):
        """`slot_uri` round-trips through `x-rdf-property` so JSON-LD
        consumers can map ``resourceType`` → ``@type`` and inherited slots
        to their RDF predicates without needing the LinkML schema."""
        assert (
            _props(schemas["Dataset"])["distribution"]["x-rdf-property"]
            == "http://www.w3.org/ns/dcat#distribution"
        )


class TestPolymorphicResponses:
    """Path operations targeting a polymorphic class emit ``oneOf`` +
    ``discriminator`` in the response (and request) schema, so a GET on
    ``/datasets/{id}`` can legitimately return a ``DatasetSeries`` or
    ``Catalog`` and the spec advertises that fact. Spring/openapi-
    generator wires this into Jackson polymorphic deserialization.
    """

    def test_get_dataset_response_is_polymorphic_oneof(self, spec):
        body = spec["paths"]["/datasets/{id}"]["get"]["responses"]["200"]
        schema = body["content"]["application/json"]["schema"]
        refs = {item["$ref"] for item in schema["oneOf"]}
        assert refs == {
            "#/components/schemas/Dataset",
            "#/components/schemas/Catalog",
            "#/components/schemas/DatasetSeries",
        }
        assert schema["discriminator"]["propertyName"] == "resourceType"

    def test_list_datasets_items_are_polymorphic(self, spec):
        body = spec["paths"]["/datasets"]["get"]["responses"]["200"]
        schema = body["content"]["application/json"]["schema"]
        assert schema["type"] == "array"
        items = schema["items"]
        refs = {item["$ref"] for item in items["oneOf"]}
        assert refs == {
            "#/components/schemas/Dataset",
            "#/components/schemas/Catalog",
            "#/components/schemas/DatasetSeries",
        }

    def test_create_dataset_request_body_accepts_subclasses(self, spec):
        body = spec["paths"]["/datasets"]["post"]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        refs = {item["$ref"] for item in schema["oneOf"]}
        assert refs == {
            "#/components/schemas/Dataset",
            "#/components/schemas/Catalog",
            "#/components/schemas/DatasetSeries",
        }

    @pytest.mark.parametrize(
        "path",
        [
            "/catalogs/{id}",
            "/data-services/{id}",
            # Distribution is `nested_only`: the canonical URL is the deep
            # chained one under its parent Catalog → Dataset, not a flat
            # `/distributions/{id}`.
            "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution/{id}",
        ],
    )
    def test_non_polymorphic_class_endpoint_uses_plain_ref(self, spec, path):
        """A class with no concrete descendants gets a bare ``$ref`` —
        no spurious oneOf wrapping."""
        body = spec["paths"][path]["get"]["responses"]["200"]
        schema = body["content"]["application/json"]["schema"]
        assert "oneOf" not in schema
        assert schema["$ref"].startswith("#/components/schemas/")


class TestPathEmission:
    def test_abstract_resource_has_no_path(self, spec):
        assert "/resources" not in spec["paths"]
        assert "/resource" not in spec["paths"]

    @pytest.mark.parametrize(
        "path",
        [
            "/datasets",
            "/datasets/{id}",
            "/catalogs",
            "/catalogs/{id}",
            "/dataset-series",
            "/dataset-series/{id}",
            "/data-services",
            "/data-services/{id}",
            # Distribution carries `openapi.nested_only: "true"` — the
            # canonical URL is the deep chain under Catalog → Dataset, and
            # the flat `/distributions[/...]` surface is suppressed.
            "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution/{id}",
            "/catalog-records",
            "/catalog-records/{id}",
        ],
    )
    def test_concrete_resource_path_exists(self, spec, path):
        assert path in spec["paths"], f"missing path: {path}"

    def test_distribution_flat_paths_suppressed_by_nested_only(self, spec):
        """``openapi.nested_only: "true"`` on Distribution drops the flat
        collection and item paths — Distributions are meaningless outside
        their parent Dataset, so the canonical URL is the deep chained one."""
        assert "/distributions" not in spec["paths"]
        assert "/distributions/{id}" not in spec["paths"]
