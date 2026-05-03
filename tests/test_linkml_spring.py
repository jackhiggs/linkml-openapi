"""Tests for the direct LinkML → Spring server emitter.

Pins the *Java source shape* against a focused set of behaviours the
emitter is responsible for:

  * polymorphism: ``is_a`` chain → Java ``extends``;
    ``openapi.discriminator`` → ``@JsonTypeInfo`` + ``@JsonSubTypes``;
    no duplicate ``resourceType`` field on the polymorphic chain
  * RDF metadata: ``class_uri`` → class-level ``@Schema(extensions=…)``
    carrying ``x-rdf-class``; ``slot_uri`` → field-level
    ``@Schema(extensions=…)`` carrying ``x-rdf-property``
  * legacy back-compat marker: ``openapi.legacy_type_field`` /
    ``…_value`` / ``…_codegen_name`` → @JsonProperty + Java field name
  * controller surface: top-level CRUD; nested CRUD for inlined slots;
    attach/detach for reference slots; pagination on lists
  * sidecar OpenAPI spec lands on the resources path

Tests work against the in-memory ``build()`` output dict, not on
disk — ~50ms per test, no Maven required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linkml_openapi.spring import SpringServerGenerator

FIXTURE = str(Path(__file__).parent / "fixtures" / "dcat3.yaml")


@pytest.fixture(scope="module")
def files() -> dict:
    """Cached in-memory rendering for every test in this module."""
    return SpringServerGenerator(FIXTURE, package="io.example.dcat").build()


# ----------------------------------------------------------------------
# Class structure
# ----------------------------------------------------------------------


class TestPolymorphism:
    """LinkML's ``is_a`` chain becomes Java ``extends``; the
    polymorphic root carries ``@JsonTypeInfo`` so Jackson dispatches
    based on the discriminator value at runtime."""

    def test_resource_is_abstract_and_carries_jsontypeinfo(self, files):
        src = files["io/example/dcat/model/Resource.java"]
        assert "public abstract class Resource" in src
        assert "@JsonTypeInfo(use = JsonTypeInfo.Id.NAME, property = \"resourceType\")" in src
        assert "@JsonSubTypes" in src

    @pytest.mark.parametrize(
        "child,parent",
        [
            ("Dataset", "Resource"),
            ("Catalog", "Dataset"),
            ("DatasetSeries", "Dataset"),
            ("DataService", "Resource"),
        ],
    )
    def test_class_extends_immediate_parent(self, files, child, parent):
        """Java inheritance mirrors LinkML ``is_a`` directly — Catalog
        extends Dataset extends Resource, not flattened to Resource."""
        src = files[f"io/example/dcat/model/{child}.java"]
        assert f"public class {child} extends {parent} {{" in src

    def test_no_duplicate_resourceType_field_on_subclass(self, files):
        """Jackson injects the discriminator via @JsonTypeInfo at
        write time. Declaring the field on subclasses produces a
        duplicate ``"resourceType":"Catalog"`` in the JSON output —
        Resource's field plus the subclass's. Suppressed in the
        generator; springdoc adds the discriminator to the schema
        from @JsonTypeInfo metadata instead."""
        for cls in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            src = files[f"io/example/dcat/model/{cls}.java"]
            assert "private String resourceType" not in src

    def test_jsonsubtypes_lists_every_concrete_descendant(self, files):
        src = files["io/example/dcat/model/Resource.java"]
        for tag in ("Dataset", "Catalog", "DatasetSeries", "DataService"):
            assert f"name = \"{tag}\")" in src


# ----------------------------------------------------------------------
# RDF metadata round-trip
# ----------------------------------------------------------------------


class TestRdfAnnotations:
    """``class_uri`` and ``slot_uri`` round-trip into Java
    ``@Schema(extensions=…)`` annotations so springdoc emits the
    ``x-rdf-class`` / ``x-rdf-property`` extensions on the live spec
    at ``/v3/api-docs``. The serdes runtime reads those from the
    spec, not from Java reflection."""

    def test_class_uri_lands_as_x_rdf_class_extension(self, files):
        src = files["io/example/dcat/model/Catalog.java"]
        assert (
            'name = "x-rdf-class"' in src
            and 'value = "http://www.w3.org/ns/dcat#Catalog"' in src
        )

    def test_class_uri_also_emitted_as_ietf_x_jsonld_type(self, files):
        """Per draft-polli-restapi-ld-keywords-02, the standardised
        keyword for the RDF type of a schema's instances is
        ``x-jsonld-type``. We emit it alongside our custom
        ``x-rdf-class`` so any IETF-draft-aware consumer picks it
        up natively without our serdes runtime in the loop."""
        src = files["io/example/dcat/model/Catalog.java"]
        assert (
            'name = "x-jsonld-type"' in src
            and 'value = "http://www.w3.org/ns/dcat#Catalog"' in src
        )

    def test_slot_uri_lands_as_x_rdf_property_extension(self, files):
        """Catalog declares a ``dataset`` slot with slot_uri:
        dcat:dataset. The Java field carries the matching
        ``@Schema(extensions=…)`` so springdoc preserves it."""
        src = files["io/example/dcat/model/Catalog.java"]
        assert 'name = "x-rdf-property"' in src
        assert 'value = "http://www.w3.org/ns/dcat#dataset"' in src

    def test_rdf_curie_appears_in_description_for_swagger_visibility(self, files):
        """The generator appends ``RDF class: \\`<curie>\\``` to the
        description so the RDF identity shows in Swagger UI's main
        text panel, not just in the (less prominent) extensions."""
        src = files["io/example/dcat/model/Catalog.java"]
        assert "RDF class: `dcat:Catalog`" in src

    def test_embedded_value_class_has_rdf_class_too(self, files):
        """Location is an embedded value class (no identifier); it
        still gets x-rdf-class so the marshaler knows the rdf:type
        for embedded sub-resources."""
        src = files["io/example/dcat/model/Location.java"]
        assert 'value = "http://purl.org/dc/terms/Location"' in src


# ----------------------------------------------------------------------
# Legacy back-compat marker (#type → legacyType)
# ----------------------------------------------------------------------


class TestLegacyTypeField:
    """``openapi.legacy_type_field: "#type"`` synthesises a Java
    field with ``@JsonProperty("#type")`` so the wire name is
    preserved while the Java identifier is sane (``legacyType``).
    The value comes from each concrete class's
    ``openapi.legacy_type_value``."""

    @pytest.mark.parametrize(
        "cls,value",
        [
            ("Dataset", "com.xyz.dcat.Dataset"),
            ("Catalog", "com.xyz.dcat.Catalog"),
            ("DatasetSeries", "com.xyz.dcat.DatasetSeries"),
            ("DataService", "com.xyz.dcat.DataService"),
        ],
    )
    def test_each_concrete_class_pins_its_legacy_value(
        self, files, cls, value
    ):
        src = files[f"io/example/dcat/model/{cls}.java"]
        assert f'private String legacyType = "{value}";' in src
        assert '@JsonProperty("#type")' in src


# ----------------------------------------------------------------------
# Controller surface
# ----------------------------------------------------------------------


class TestApiSurface:
    """Each ``openapi.resource: "true"`` class gets a Spring
    interface with top-level CRUD plus per-slot nested or attach/
    detach operations."""

    def test_resource_classes_get_api_interfaces(self, files):
        for cls in (
            "Agent",
            "Catalog",
            "CatalogRecord",
            "DataService",
            "Dataset",
            "DatasetSeries",
            "Distribution",
        ):
            assert f"io/example/dcat/api/{cls}Api.java" in files

    def test_top_level_crud_present(self, files):
        src = files["io/example/dcat/api/CatalogApi.java"]
        assert "@GetMapping(value = \"/catalogs\"" in src
        assert "@PostMapping(value = \"/catalogs\"" in src
        assert "@GetMapping(value = \"/catalogs/{id}\"" in src
        assert "@PutMapping(value = \"/catalogs/{id}\"" in src
        assert "@DeleteMapping(\"/catalogs/{id}\")" in src

    def test_list_endpoints_have_paging_query_params(self, files):
        src = files["io/example/dcat/api/CatalogApi.java"]
        assert 'name = "limit"' in src and 'defaultValue = "50"' in src
        assert 'name = "offset"' in src and 'defaultValue = "0"' in src

    def test_inlined_composition_emits_nested_crud(self, files):
        """Dataset.distribution is inlined: true → nested CRUD on
        /datasets/{id}/distribution + /datasets/{id}/distribution/{id}.
        Methods reflect the embedded payload type (Distribution)."""
        src = files["io/example/dcat/api/DatasetApi.java"]
        assert "@GetMapping(value = \"/datasets/{id}/distribution\"" in src
        assert "@PostMapping(value = \"/datasets/{id}/distribution\"" in src
        assert "@GetMapping(value = \"/datasets/{id}/distribution/{DistributionId}\"" in src
        assert "ResponseEntity<Distribution>" in src
        assert "List<Distribution>" in src

    def test_reference_slot_emits_attach_detach(self, files):
        """Catalog.dataset is inlined: false → attach (POST IRI body)
        and detach (DELETE) — no full lifecycle on the relationship."""
        src = files["io/example/dcat/api/CatalogApi.java"]
        assert "attachCatalogDataset" in src
        assert "detachCatalogDataset" in src
        assert "@RequestBody URI targetIri" in src
        # Reference list returns IRIs, not embedded objects.
        assert "List<URI>" in src

    def test_no_nested_paths_for_embedded_value_classes(self, files):
        """Location/PeriodOfTime/Checksum have no identifier — they're
        composition-only embedded values. No /{id}/spatial endpoint;
        the data lives inside the parent resource's representation."""
        src = files["io/example/dcat/api/DatasetApi.java"]
        assert "/spatial" not in src
        assert "/temporal" not in src

    def test_media_types_advertised_on_resource_endpoints(self, files):
        """``openapi.media_types`` flows into ``produces`` /
        ``consumes`` arrays so springdoc advertises content
        negotiation."""
        src = files["io/example/dcat/api/CatalogApi.java"]
        assert '"application/json"' in src
        assert '"application/ld+json"' in src
        assert '"text/turtle"' in src
        assert '"application/rdf+xml"' in src

    def test_problem_responses_declared_on_every_op(self, files):
        src = files["io/example/dcat/api/CatalogApi.java"]
        # Every op carries the Problem error contract for 404/422/500.
        assert src.count('responseCode = "404"') >= 1
        assert src.count('responseCode = "422"') >= 1
        assert src.count('responseCode = "500"') >= 1
        assert "Problem.class" in src


# ----------------------------------------------------------------------
# Sidecar spec emission
# ----------------------------------------------------------------------


class TestSidecarSpec:
    """``gen-spring-server`` writes the canonical OpenAPI spec to a
    sibling ``resources/openapi.yaml`` so the runtime serdes layer
    can load it from the classpath without parsing the LinkML
    schema itself."""

    def test_emit_writes_sidecar_spec(self, tmp_path):
        java_dir = tmp_path / "java"
        gen = SpringServerGenerator(FIXTURE, package="io.example.dcat")
        written = gen.emit(java_dir)
        spec_path = tmp_path / "resources" / "openapi.yaml"
        assert spec_path in written
        spec = spec_path.read_text()
        assert "x-rdf-class" in spec
        assert "x-rdf-property" in spec


# ----------------------------------------------------------------------
# Validation & types
# ----------------------------------------------------------------------


class TestValidationAndTypes:
    def test_required_slots_carry_notnull(self, files):
        """LinkML ``required: true`` → Jakarta Bean Validation
        ``@NotNull`` on the Java field. Spring's @Validated runs
        these at request-binding time."""
        src = files["io/example/dcat/model/DataService.java"]
        # endpointURL is required: true on DataService
        assert "@NotNull" in src
        assert "private java.net.URI endpointURL" in src

    def test_uri_ranges_become_uri_typed(self, files):
        """RDF-link slots use java.net.URI rather than String — Java
        type advertises 'this is an IRI'."""
        src = files["io/example/dcat/model/Resource.java"]
        assert "private java.net.URI landingPage" in src
        assert "private java.net.URI license" in src

    def test_datetime_slots_become_offset_date_time(self, files):
        src = files["io/example/dcat/model/Resource.java"]
        assert "private java.time.OffsetDateTime issued" in src
        assert "private java.time.OffsetDateTime modified" in src


# ----------------------------------------------------------------------
# Slot-driven query parameters on list endpoints
# ----------------------------------------------------------------------


QP_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_query_params.yaml")


@pytest.fixture(scope="module")
def qp_files() -> dict:
    return SpringServerGenerator(QP_FIXTURE, package="io.example.qp").build()


class TestQueryParams:
    """Slot-driven @RequestParam emission on Spring controllers."""

    def test_equality_param_emitted(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert '@RequestParam(name = "active", required = false) Boolean active' in src

    def test_comparable_emits_four_suffix_params(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert '@RequestParam(name = "age", required = false) Long age' in src
        assert '@RequestParam(name = "age__gte", required = false) Long ageGte' in src
        assert '@RequestParam(name = "age__lte", required = false) Long ageLte' in src
        assert '@RequestParam(name = "age__gt", required = false) Long ageGt' in src
        assert '@RequestParam(name = "age__lt", required = false) Long ageLt' in src

    def test_sortable_emits_single_list_string_param(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert (
            '@RequestParam(name = "sort", required = false) java.util.List<String> sort'
            in src
        )

    def test_query_param_false_excludes_slot(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert '"email"' not in src

    def test_query_param_java_types_per_range(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert (
            '@RequestParam(name = "created__gte", required = false) '
            'java.time.OffsetDateTime createdGte' in src
        )
        assert 'Boolean active' in src
        assert 'Long age' in src

    def test_paging_params_still_present(self, qp_files):
        src = qp_files["io/example/qp/api/PersonApi.java"]
        assert 'name = "limit"' in src
        assert 'name = "offset"' in src

    def test_query_params_attached_to_composition_list(self, files):
        """Dataset.distribution is inlined: true → composition list at
        /datasets/{id}/distribution. The list endpoint carries Distribution's
        query params (auto-inferred from Distribution's scalar slots)."""
        src = files["io/example/dcat/api/DatasetApi.java"]
        assert "/datasets/{id}/distribution" in src
        list_method_start = src.find('listDatasetDistribution')
        list_method_end = src.find(') {', list_method_start)
        list_method_signature = src[list_method_start:list_method_end]
        assert list_method_signature.count("@RequestParam") >= 3

    def test_query_params_attached_to_reference_list(self, files):
        """Catalog.dataset is inlined: false → reference list at
        /catalogs/{id}/dataset. The list of attached IRIs carries Dataset's
        query params."""
        src = files["io/example/dcat/api/CatalogApi.java"]
        list_method_start = src.find('listCatalogDatasetRefs')
        list_method_end = src.find(') {', list_method_start)
        list_method_signature = src[list_method_start:list_method_end]
        assert list_method_signature.count("@RequestParam") >= 3

    def test_unknown_query_param_token_warns(self, tmp_path):
        fixture = tmp_path / "schema.yaml"
        fixture.write_text("""\
id: https://example.org/qp_warn
name: qp_warn
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id: { identifier: true, range: string, required: true }
      name: { range: string }
    slot_usage:
      name:
        annotations:
          openapi.query_param: sorteable
""")
        with pytest.warns(UserWarning, match="sorteable"):
            SpringServerGenerator(str(fixture), package="io.example.qp_warn").build()

    def test_sortable_on_multivalued_raises(self, tmp_path):
        fixture = tmp_path / "schema.yaml"
        fixture.write_text("""\
id: https://example.org/qp_err
name: qp_err
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Person:
    annotations: { openapi.resource: "true", openapi.path: people }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: string, multivalued: true }
    slot_usage:
      tags:
        annotations:
          openapi.query_param: sortable
""")
        with pytest.raises(ValueError, match="multivalued"):
            SpringServerGenerator(str(fixture), package="io.example.qp_err").build()


class TestPathStyle:
    """openapi.path_style: kebab-case + per-slot openapi.path_segment.
    Spring's auto-derived class path segments and slot segments respect
    the active path style; explicit per-class openapi.path values are
    taken verbatim (no transformation)."""

    def test_kebab_case_class_path_for_camelcase_class(self, tmp_path):
        fixture = tmp_path / "kebab.yaml"
        fixture.write_text("""\
id: https://example.org/k
name: k
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
annotations:
  openapi.path_style: kebab-case
classes:
  DataService:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.k").build()
        src = files["io/example/k/api/DataServiceApi.java"]
        assert '@GetMapping(value = "/data-services",' in src
        assert '@GetMapping(value = "/data-services/{id}",' in src

    def test_explicit_openapi_path_taken_verbatim(self, tmp_path):
        """openapi.path on a class is verbatim — no path-style transform."""
        fixture = tmp_path / "verbatim.yaml"
        fixture.write_text("""\
id: https://example.org/v
name: v
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path: my-custom-path
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.v").build()
        src = files["io/example/v/api/FooApi.java"]
        assert '@GetMapping(value = "/my-custom-path",' in src

    def test_slot_path_segment_override(self, tmp_path):
        """openapi.path_segment on a slot is taken verbatim and lands on
        nested URLs even when the slot identifier in the model stays
        snake_case."""
        fixture = tmp_path / "ps.yaml"
        fixture.write_text("""\
id: https://example.org/ps
name: ps
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Hub:
    annotations: { openapi.resource: "true", openapi.path: hubs }
    attributes:
      id: { identifier: true, range: string, required: true }
      web_resources:
        range: WebResource
        multivalued: true
        inlined: true
    slot_usage:
      web_resources:
        annotations:
          openapi.path_segment: "web-resources"
  WebResource:
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.ps").build()
        src = files["io/example/ps/api/HubApi.java"]
        assert '@GetMapping(value = "/hubs/{id}/web-resources",' in src

    def test_kebab_applied_to_nested_slot_segments(self, tmp_path):
        """Schema-level kebab-case applies to auto-derived nested slot
        segments — /things/{id}/sub-things."""
        fixture = tmp_path / "k_nested.yaml"
        fixture.write_text("""\
id: https://example.org/kn
name: kn
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
annotations:
  openapi.path_style: kebab-case
classes:
  Thing:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      sub_things:
        range: SubThing
        multivalued: true
        inlined: true
  SubThing:
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.kn").build()
        src = files["io/example/kn/api/ThingApi.java"]
        # Class path "things" stays unchanged (no underscores); slot
        # segment "sub_things" becomes "sub-things" under kebab style.
        assert '@GetMapping(value = "/things/{id}/sub-things",' in src


class TestDeepChainedPaths:
    """Deep nested URL chains land on the leaf class's controller as
    item-only operations (read/update/delete) on the deep item path.
    Mirrors the OpenAPI generator's _emit_chained_deep_path which calls
    only _attach_item_operations."""

    def test_deep_path_emits_on_distribution_api(self, files):
        """Distribution's chain is [(Catalog, dataset), (Dataset, distribution)].
        Singular slot names per dcat3; default <class_snake>_id ancestors;
        leaf is {id}."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution/{id}"'
            in src
        )

    def test_deep_method_name_via_chain_suffix(self, files):
        src = files["io/example/dcat/api/DistributionApi.java"]
        assert "getDistributionViaCatalogDataset" in src
        assert "updateDistributionViaCatalogDataset" in src
        assert "deleteDistributionViaCatalogDataset" in src

    def test_dataset_chain_depth_one(self, files):
        """Dataset's parent chain is just [(Catalog, dataset)] — a single
        hop. The deep URL is /catalogs/{catalog_id}/dataset/{id}."""
        src = files["io/example/dcat/api/DatasetApi.java"]
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{id}"'
            in src
        )
        assert "getDatasetViaCatalog" in src

    def test_deep_path_params_in_order(self, files):
        """Path parameters declared root → leaf in the method signature."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        get_idx = src.find("getDistributionViaCatalogDataset")
        sig_end = src.find(") {", get_idx)
        signature = src[get_idx:sig_end]
        assert (
            signature.index('"catalog_id"')
            < signature.index('"dataset_id"')
            < signature.index('"id"')
        )

    def test_deep_chained_url_is_item_only(self, files):
        """No collection-level GET/POST on the deep chained URL."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        assert (
            '@PostMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution",'
            not in src
        )
        assert (
            '@GetMapping(value = "/catalogs/{catalog_id}/dataset/{dataset_id}/distribution",'
            not in src
        )

    def test_no_method_name_collision_between_flat_and_deep_ops(self, files):
        """Within DistributionApi, all method names must be unique."""
        src = files["io/example/dcat/api/DistributionApi.java"]
        import re
        method_names = re.findall(r"default ResponseEntity<[^>]*> (\w+)\(", src)
        assert len(method_names) == len(set(method_names)), (
            f"duplicate methods: {[m for m in method_names if method_names.count(m) > 1]}"
        )

    def test_ancestor_path_id_annotation_honored(self, tmp_path):
        """When an ancestor class declares openapi.path_id, the chain
        URL uses that value instead of the <class_snake>_id default."""
        fixture = tmp_path / "path_id.yaml"
        fixture.write_text("""\
id: https://example.org/pid
name: pid
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations:
      openapi.resource: "true"
      openapi.path: catalogs
      openapi.path_id: catId
    attributes:
      id: { identifier: true, range: string, required: true }
      datasets:
        range: Dataset
        multivalued: true
  Dataset:
    annotations:
      openapi.resource: "true"
      openapi.path: datasets
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.pid").build()
        src = files["io/example/pid/api/DatasetApi.java"]
        assert '@GetMapping(value = "/catalogs/{catId}/datasets/{id}"' in src
        assert '@PathVariable("catId")' in src

    def test_ambiguous_chain_without_parent_path_raises(self, tmp_path):
        fixture = tmp_path / "ambig.yaml"
        fixture.write_text("""\
id: https://example.org/amb
name: amb
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Folder:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: Tag, multivalued: true }
  Bookmark:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
      tags: { range: Tag, multivalued: true }
  Tag:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="multiple parent chains"):
            SpringServerGenerator(str(fixture), package="io.example.amb").build()


NO_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_nested_only.yaml")


@pytest.fixture(scope="module")
def no_files() -> dict:
    return SpringServerGenerator(NO_FIXTURE, package="io.example.no_").build()


class TestNestedOnlyAndFlatOnly:
    def test_nested_only_suppresses_flat_ops(self, no_files):
        src = no_files["io/example/no_/api/DatasetApi.java"]
        # No flat /datasets endpoints.
        assert '@GetMapping(value = "/datasets",' not in src
        assert '@GetMapping(value = "/datasets/{id}",' not in src
        # Deep chain endpoint IS present (slot `datasets` plural in this
        # fixture; ancestor uses default snake_case path_id; leaf is {id}).
        assert '/catalogs/{catalog_id}/datasets/{id}' in src

    def test_flat_only_suppresses_deep_ops(self, no_files):
        src = no_files["io/example/no_/api/TagApi.java"]
        # Flat endpoints present.
        assert '@GetMapping(value = "/tags",' in src
        assert '@GetMapping(value = "/tags/{id}",' in src
        # Deep chain endpoint NOT present.
        assert '/catalogs/{catalog_id}/tags/{id}' not in src

    def test_nested_only_and_flat_only_together_raises(self, tmp_path):
        fixture = tmp_path / "bad.yaml"
        fixture.write_text("""\
id: https://example.org/bad
name: bad
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.nested_only: "true"
      openapi.flat_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="mutually exclusive"):
            SpringServerGenerator(str(fixture), package="io.example.bad").build()


TPL_FIXTURE = str(Path(__file__).parent / "fixtures" / "spring_path_template.yaml")


@pytest.fixture(scope="module")
def tpl_files() -> dict:
    return SpringServerGenerator(TPL_FIXTURE, package="io.example.tpl").build()


class TestTemplatedPaths:
    def test_template_emits_verbatim_url(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert (
            '@GetMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}/{version}"'
            in src
        )

    def test_template_method_name_via_template_suffix(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert "getResourceVersionViaTemplate" in src
        assert "updateResourceVersionViaTemplate" in src
        assert "deleteResourceVersionViaTemplate" in src

    def test_template_path_params_typed_from_sources(self, tpl_files):
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        # All three sources resolve to string-ranged slots → String params.
        assert '@PathVariable("cId") String cId' in src
        assert '@PathVariable("doi") String doi' in src
        assert '@PathVariable("version") String version' in src

    def test_template_collection_emitted_when_ending_in_placeholder(self, tpl_files):
        """The template ends with /{version}; default-on collection emits
        list/create at /v2/catalogs/{cId}/resources/by-doi/{doi}."""
        src = tpl_files["io/example/tpl/api/ResourceVersionApi.java"]
        assert (
            '@GetMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}",'
            in src
        )
        assert (
            '@PostMapping(value = "/v2/catalogs/{cId}/resources/by-doi/{doi}",'
            in src
        )

    def test_template_placeholder_source_mismatch_raises(self, tmp_path):
        fixture = tmp_path / "tpl_bad.yaml"
        fixture.write_text("""\
id: https://example.org/tpl_bad
name: tpl_bad
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/{a}/{b}"
      openapi.path_param_sources: "a:Catalog.id"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="don't match"):
            SpringServerGenerator(str(fixture), package="io.example.tpl_bad").build()

    def test_template_unknown_source_class_raises(self, tmp_path):
        fixture = tmp_path / "tpl_unknown.yaml"
        fixture.write_text("""\
id: https://example.org/tpl_u
name: tpl_u
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Foo:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/{a}"
      openapi.path_param_sources: "a:DoesNotExist.id"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        with pytest.raises(ValueError, match="unknown class"):
            SpringServerGenerator(str(fixture), package="io.example.tpl_u").build()

    def test_template_collection_opt_out(self, tmp_path):
        fixture = tmp_path / "tpl_no_coll.yaml"
        fixture.write_text("""\
id: https://example.org/tpl_nc
name: tpl_nc
prefixes: { linkml: https://w3id.org/linkml/ }
default_range: string
classes:
  Catalog:
    annotations: { openapi.resource: "true" }
    attributes:
      id: { identifier: true, range: string, required: true }
  ResourceVersion:
    annotations:
      openapi.resource: "true"
      openapi.path_template: "/v2/catalogs/{cId}/resources/{rId}"
      openapi.path_param_sources: "cId:Catalog.id,rId:ResourceVersion.id"
      openapi.path_template_collection: "false"
      openapi.nested_only: "true"
    attributes:
      id: { identifier: true, range: string, required: true }
""")
        files = SpringServerGenerator(str(fixture), package="io.example.tpl_nc").build()
        src = files["io/example/tpl_nc/api/ResourceVersionApi.java"]
        # Item path emits.
        assert "/v2/catalogs/{cId}/resources/{rId}" in src
        # No collection /v2/catalogs/{cId}/resources GET or POST.
        assert '@GetMapping(value = "/v2/catalogs/{cId}/resources",' not in src
        assert '@PostMapping(value = "/v2/catalogs/{cId}/resources",' not in src
