package io.example.dcat;

import io.example.dcat.api.AgentApi;
import io.example.dcat.api.CatalogApi;
import io.example.dcat.api.CatalogRecordApi;
import io.example.dcat.api.DataServiceApi;
import io.example.dcat.api.DatasetApi;
import io.example.dcat.api.DatasetSeriesApi;
import io.example.dcat.api.DistributionApi;
import io.example.dcat.model.Agent;
import io.example.dcat.model.Catalog;
import io.example.dcat.model.CatalogRecord;
import io.example.dcat.model.DataService;
import io.example.dcat.model.Distribution;
import io.example.dcat.model.Dataset;
import io.example.dcat.model.DatasetSeries;
import io.example.dcat.store.InMemoryStore;
import io.example.dcat.store.Stores;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/**
 * Concrete @RestController beans backing the generated *Api
 * interfaces with the in-memory {@link Stores} so the demo serves
 * actual DCAT-3 data — list / read / create / update / delete on
 * every top-level resource.
 *
 * <p>RDF content negotiation flows through transparently: the
 * controllers return Java DTOs; the spec-driven
 * {@link io.linkmlspring.rdf.RdfMapper} marshals them to Turtle /
 * JSON-LD / RDF/XML based on the {@code Accept} header. The
 * controllers know nothing about RDF.
 *
 * <p>Nested CRUD and attach/detach paths still 501 — those
 * relationship operations are out of scope for the seed-data
 * demo.
 */
@Configuration
public class StubControllers {

    /**
     * DCAT resources are addressed by full IRI (the URL is the
     * identity). The Spring path parameter receives just the URL
     * suffix (e.g. {@code city-data}); we prepend a base URI to
     * build the full IRI before the store lookup. Hardcoded here
     * for demo simplicity — a real service would pull this from
     * configuration or the request's host header.
     */
    private static final String BASE = "https://example.org/";

    private static String iri(String type, String id) {
        return BASE + type + "/" + id;
    }

    private static <T> ResponseEntity<T> readOr404(
            InMemoryStore<T> store, String fullId) {
        return store.get(fullId)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    private static <T> ResponseEntity<Void> deleteOr404(
            InMemoryStore<T> store, String fullId) {
        return store.remove(fullId)
                ? ResponseEntity.noContent().build()
                : ResponseEntity.notFound().build();
    }

    // -----------------------------------------------------------------

    @RestController
    public static class CatalogController implements CatalogApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<Catalog>> listCatalogs(
                Integer limit, Integer offset,
                String title,
                java.time.OffsetDateTime issued,
                java.time.OffsetDateTime issuedGte,
                java.time.OffsetDateTime issuedLte,
                java.time.OffsetDateTime issuedGt,
                java.time.OffsetDateTime issuedLt,
                java.time.OffsetDateTime modified,
                java.time.OffsetDateTime modifiedGte,
                java.time.OffsetDateTime modifiedLte,
                java.time.OffsetDateTime modifiedGt,
                java.time.OffsetDateTime modifiedLt,
                java.util.List<String> sort) {
            return ResponseEntity.ok(stores.catalogs.list(limit, offset));
        }

        @Override
        public ResponseEntity<Catalog> getCatalog(String id) {
            return readOr404(stores.catalogs, iri("catalogs", id));
        }

        @Override
        public ResponseEntity<Catalog> createCatalog(Catalog body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.catalogs.put(body));
        }

        @Override
        public ResponseEntity<Catalog> updateCatalog(
                String id, Catalog body) {
            body.setId(iri("catalogs", id));
            return ResponseEntity.ok(stores.catalogs.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteCatalog(String id) {
            return deleteOr404(stores.catalogs, iri("catalogs", id));
        }
    }

    @RestController
    public static class DatasetController implements DatasetApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<Dataset>> listDatasets(
                Integer limit, Integer offset,
                String title,
                java.time.OffsetDateTime issued,
                java.time.OffsetDateTime issuedGte,
                java.time.OffsetDateTime issuedLte,
                java.time.OffsetDateTime issuedGt,
                java.time.OffsetDateTime issuedLt,
                java.time.OffsetDateTime modified,
                java.time.OffsetDateTime modifiedGte,
                java.time.OffsetDateTime modifiedLte,
                java.time.OffsetDateTime modifiedGt,
                java.time.OffsetDateTime modifiedLt,
                java.util.List<String> sort) {
            return ResponseEntity.ok(stores.datasets.list(limit, offset));
        }

        @Override
        public ResponseEntity<Dataset> getDataset(String id) {
            return readOr404(stores.datasets, iri("datasets", id));
        }

        @Override
        public ResponseEntity<Dataset> createDataset(Dataset body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.datasets.put(body));
        }

        @Override
        public ResponseEntity<Dataset> updateDataset(
                String id, Dataset body) {
            body.setId(iri("datasets", id));
            return ResponseEntity.ok(stores.datasets.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteDataset(String id) {
            return deleteOr404(stores.datasets, iri("datasets", id));
        }
    }

    @RestController
    public static class DatasetSeriesController implements DatasetSeriesApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<DatasetSeries>> listDatasetSeriess(
                Integer limit, Integer offset,
                String temporalResolution,
                String title,
                String description,
                String rights,
                String version,
                String versionNotes) {
            return ResponseEntity.ok(
                    stores.datasetSeries.list(limit, offset));
        }

        @Override
        public ResponseEntity<DatasetSeries> getDatasetSeries(String id) {
            return readOr404(stores.datasetSeries, iri("dataset_series", id));
        }

        @Override
        public ResponseEntity<DatasetSeries> createDatasetSeries(
                DatasetSeries body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.datasetSeries.put(body));
        }

        @Override
        public ResponseEntity<DatasetSeries> updateDatasetSeries(
                String id, DatasetSeries body) {
            body.setId(iri("dataset_series", id));
            return ResponseEntity.ok(stores.datasetSeries.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteDatasetSeries(String id) {
            return deleteOr404(stores.datasetSeries, iri("dataset_series", id));
        }
    }

    @RestController
    public static class DataServiceController implements DataServiceApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<DataService>> listDataServices(
                Integer limit, Integer offset,
                String title,
                String description,
                String rights,
                String version,
                String versionNotes) {
            return ResponseEntity.ok(
                    stores.dataServices.list(limit, offset));
        }

        @Override
        public ResponseEntity<DataService> getDataService(String id) {
            return readOr404(stores.dataServices, iri("data_services", id));
        }

        @Override
        public ResponseEntity<DataService> createDataService(
                DataService body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.dataServices.put(body));
        }

        @Override
        public ResponseEntity<DataService> updateDataService(
                String id, DataService body) {
            body.setId(iri("data_services", id));
            return ResponseEntity.ok(stores.dataServices.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteDataService(String id) {
            return deleteOr404(stores.dataServices, iri("data_services", id));
        }
    }

    @RestController
    public static class CatalogRecordController implements CatalogRecordApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<CatalogRecord>> listCatalogRecords(
                Integer limit, Integer offset,
                String title,
                java.time.OffsetDateTime listingDate,
                java.time.OffsetDateTime listingDateGte,
                java.time.OffsetDateTime listingDateLte,
                java.time.OffsetDateTime listingDateGt,
                java.time.OffsetDateTime listingDateLt,
                java.util.List<String> sort) {
            return ResponseEntity.ok(
                    stores.catalogRecords.list(limit, offset));
        }

        @Override
        public ResponseEntity<CatalogRecord> getCatalogRecord(String id) {
            return readOr404(stores.catalogRecords, iri("catalog_records", id));
        }

        @Override
        public ResponseEntity<CatalogRecord> createCatalogRecord(
                CatalogRecord body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.catalogRecords.put(body));
        }

        @Override
        public ResponseEntity<CatalogRecord> updateCatalogRecord(
                String id, CatalogRecord body) {
            body.setId(iri("catalog_records", id));
            return ResponseEntity.ok(stores.catalogRecords.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteCatalogRecord(String id) {
            return deleteOr404(stores.catalogRecords, iri("catalog_records", id));
        }
    }

    @RestController
    public static class AgentController implements AgentApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<List<Agent>> listAgents(
                Integer limit, Integer offset,
                String name) {
            return ResponseEntity.ok(stores.agents.list(limit, offset));
        }

        @Override
        public ResponseEntity<Agent> getAgent(String id) {
            return readOr404(stores.agents, iri("agents", id));
        }

        @Override
        public ResponseEntity<Agent> createAgent(Agent body) {
            return ResponseEntity.status(HttpStatus.CREATED)
                    .body(stores.agents.put(body));
        }

        @Override
        public ResponseEntity<Agent> updateAgent(
                String id, Agent body) {
            body.setId(iri("agents", id));
            return ResponseEntity.ok(stores.agents.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteAgent(String id) {
            return deleteOr404(stores.agents, iri("agents", id));
        }
    }

    /**
     * Distribution is `nested_only` — its only canonical URLs are the
     * deep chain `/catalogs/{cat}/dataset/{ds}/distribution/{id}`.
     * Wires through to the same in-memory store as the parent paths.
     */
    @RestController
    public static class DistributionController implements DistributionApi {
        @Autowired private Stores stores;

        @Override
        public ResponseEntity<Distribution> getDistributionViaCatalogDataset(
                String catalog_id, String dataset_id, String id) {
            return readOr404(stores.distributions, iri("distributions", id));
        }

        @Override
        public ResponseEntity<Distribution> updateDistributionViaCatalogDataset(
                String catalog_id, String dataset_id, String id, Distribution body) {
            body.setId(iri("distributions", id));
            return ResponseEntity.ok(stores.distributions.put(body));
        }

        @Override
        public ResponseEntity<Void> deleteDistributionViaCatalogDataset(
                String catalog_id, String dataset_id, String id) {
            return deleteOr404(stores.distributions, iri("distributions", id));
        }
    }
}
