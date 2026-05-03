package io.example.dcat.store;

import io.example.dcat.model.Agent;
import io.example.dcat.model.Catalog;
import io.example.dcat.model.CatalogRecord;
import io.example.dcat.model.DataService;
import io.example.dcat.model.Dataset;
import io.example.dcat.model.DatasetSeries;
import io.example.dcat.model.Distribution;
import jakarta.annotation.PostConstruct;
import java.net.URI;
import java.time.OffsetDateTime;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * Spring-managed registry of one {@link InMemoryStore} per resource
 * type, seeded at startup with a small but interconnected DCAT-3
 * dataset so the live demo is browsable rather than empty.
 *
 * <p>Seed shape: one root Catalog ("city-data"), two Datasets
 * (transit-2024 and budget-2024 — one with two embedded
 * Distributions, one in a DatasetSeries), one DataService, two
 * Agents (publisher + a creator), one CatalogRecord, one
 * DatasetSeries (annual-budgets). Cross-references use absolute
 * IRIs so RDF/XML pretty-print works.
 */
@Component
public class Stores {

    public final InMemoryStore<Catalog> catalogs =
            new InMemoryStore<>(Catalog::getId);
    public final InMemoryStore<Dataset> datasets =
            new InMemoryStore<>(Dataset::getId);
    public final InMemoryStore<DatasetSeries> datasetSeries =
            new InMemoryStore<>(DatasetSeries::getId);
    public final InMemoryStore<DataService> dataServices =
            new InMemoryStore<>(DataService::getId);
    public final InMemoryStore<Distribution> distributions =
            new InMemoryStore<>(Distribution::getId);
    public final InMemoryStore<CatalogRecord> catalogRecords =
            new InMemoryStore<>(CatalogRecord::getId);
    public final InMemoryStore<Agent> agents =
            new InMemoryStore<>(Agent::getId);

    @PostConstruct
    public void seed() {
        // --- Agents ---
        Agent cityDataOffice = new Agent();
        cityDataOffice.setId(
                "https://example.org/agents/city-data-office");
        cityDataOffice.setName("City Data Office");
        cityDataOffice.setHomepage(URI.create(
                "https://city.example.org/data-office"));
        cityDataOffice.setMbox(URI.create(
                "mailto:data@city.example.org"));
        agents.put(cityDataOffice);

        Agent transitAgency = new Agent();
        transitAgency.setId(
                "https://example.org/agents/transit-agency");
        transitAgency.setName("Metropolitan Transit Agency");
        transitAgency.setHomepage(URI.create(
                "https://transit.example.org"));
        agents.put(transitAgency);

        // --- DatasetSeries ---
        DatasetSeries annualBudgets = new DatasetSeries();
        annualBudgets.setId(
                "https://example.org/dataset_series/annual-budgets");
        annualBudgets.setTitle("Annual Budget Series");
        annualBudgets.setDescription(
                "Yearly publication of the city's enacted budget,"
                        + " 2018-present.");
        annualBudgets.setIssued(
                OffsetDateTime.parse("2018-01-01T00:00:00Z"));
        annualBudgets.setPublisher(URI.create(
                "https://example.org/agents/city-data-office"));
        datasetSeries.put(annualBudgets);

        // --- DataService ---
        DataService transitApi = new DataService();
        transitApi.setId(
                "https://example.org/data_services/transit-api");
        transitApi.setTitle("Transit Real-time API");
        transitApi.setDescription(
                "GTFS-realtime feed with vehicle positions,"
                        + " trip updates, and service alerts.");
        transitApi.setEndpointURL(URI.create(
                "https://transit.example.org/api/v1"));
        transitApi.setEndpointDescription(URI.create(
                "https://transit.example.org/api/v1/openapi.yaml"));
        transitApi.setServesDataset(List.of(URI.create(
                "https://example.org/datasets/transit-2024")));
        transitApi.setPublisher(URI.create(
                "https://example.org/agents/transit-agency"));
        dataServices.put(transitApi);

        // --- Distributions (embedded under Dataset, but also stored
        //     standalone so /distributions endpoints work) ---
        Distribution transitGtfsZip = new Distribution();
        transitGtfsZip.setId(
                "https://example.org/distributions/transit-2024-gtfs");
        transitGtfsZip.setTitle("GTFS schedule (zipped)");
        transitGtfsZip.setDescription(
                "Static schedule data in GTFS format, "
                        + "ZIP-packaged.");
        transitGtfsZip.setAccessURL(URI.create(
                "https://transit.example.org/gtfs/2024.zip"));
        transitGtfsZip.setDownloadURL(URI.create(
                "https://transit.example.org/gtfs/2024.zip"));
        transitGtfsZip.setMediaType(URI.create(
                "https://www.iana.org/assignments/media-types/application/zip"));
        transitGtfsZip.setByteSize(8_452_096L);
        transitGtfsZip.setLicense(URI.create(
                "https://creativecommons.org/publicdomain/zero/1.0/"));
        distributions.put(transitGtfsZip);

        Distribution transitGtfsCsv = new Distribution();
        transitGtfsCsv.setId(
                "https://example.org/distributions/transit-2024-csv");
        transitGtfsCsv.setTitle("GTFS schedule (CSV bundle)");
        transitGtfsCsv.setAccessURL(URI.create(
                "https://transit.example.org/gtfs/2024-csv/"));
        transitGtfsCsv.setMediaType(URI.create(
                "https://www.iana.org/assignments/media-types/text/csv"));
        transitGtfsCsv.setByteSize(24_117_248L);
        distributions.put(transitGtfsCsv);

        Distribution budgetCsv = new Distribution();
        budgetCsv.setId(
                "https://example.org/distributions/budget-2024-csv");
        budgetCsv.setTitle("Enacted budget — line items (CSV)");
        budgetCsv.setAccessURL(URI.create(
                "https://city.example.org/budget/2024.csv"));
        budgetCsv.setMediaType(URI.create(
                "https://www.iana.org/assignments/media-types/text/csv"));
        budgetCsv.setByteSize(512_000L);
        budgetCsv.setLicense(URI.create(
                "https://creativecommons.org/licenses/by/4.0/"));
        distributions.put(budgetCsv);

        // --- Datasets ---
        Dataset transit = new Dataset();
        transit.setId("https://example.org/datasets/transit-2024");
        transit.setTitle("Transit schedule 2024");
        transit.setDescription(
                "Bus, rail, and ferry schedules for the city's "
                        + "transit network, calendar year 2024.");
        transit.setIssued(
                OffsetDateTime.parse("2024-01-01T00:00:00Z"));
        transit.setModified(
                OffsetDateTime.parse("2024-12-15T00:00:00Z"));
        transit.setKeyword(List.of(
                "transit", "schedule", "gtfs", "public-transport"));
        transit.setLicense(URI.create(
                "https://creativecommons.org/publicdomain/zero/1.0/"));
        transit.setPublisher(URI.create(
                "https://example.org/agents/transit-agency"));
        transit.setDistribution(List.of(transitGtfsZip, transitGtfsCsv));
        datasets.put(transit);

        Dataset budget = new Dataset();
        budget.setId("https://example.org/datasets/budget-2024");
        budget.setTitle("Enacted budget 2024");
        budget.setDescription(
                "Line-item enacted budget for fiscal year 2024, "
                        + "all city departments.");
        budget.setIssued(
                OffsetDateTime.parse("2024-07-01T00:00:00Z"));
        budget.setKeyword(List.of("budget", "finance", "fiscal-2024"));
        budget.setLicense(URI.create(
                "https://creativecommons.org/licenses/by/4.0/"));
        budget.setPublisher(URI.create(
                "https://example.org/agents/city-data-office"));
        budget.setInSeries(List.of(URI.create(
                "https://example.org/dataset_series/annual-budgets")));
        budget.setDistribution(List.of(budgetCsv));
        datasets.put(budget);

        // --- Catalog ---
        Catalog cityData = new Catalog();
        cityData.setId("https://example.org/catalogs/city-data");
        cityData.setTitle("City Open Data Portal");
        cityData.setDescription(
                "A curated index of open datasets and data services "
                        + "published by city departments.");
        cityData.setIssued(
                OffsetDateTime.parse("2018-01-15T00:00:00Z"));
        cityData.setModified(OffsetDateTime.now());
        cityData.setKeyword(List.of(
                "open-data", "government", "civic"));
        cityData.setLandingPage(URI.create(
                "https://city.example.org/data"));
        cityData.setLicense(URI.create(
                "https://creativecommons.org/publicdomain/zero/1.0/"));
        cityData.setPublisher(URI.create(
                "https://example.org/agents/city-data-office"));
        cityData.setDataset(List.of(
                URI.create("https://example.org/datasets/transit-2024"),
                URI.create("https://example.org/datasets/budget-2024")));
        cityData.setService(List.of(URI.create(
                "https://example.org/data_services/transit-api")));
        cityData.setHomepage(URI.create(
                "https://city.example.org/data"));
        catalogs.put(cityData);

        // --- CatalogRecord ---
        CatalogRecord transitRecord = new CatalogRecord();
        transitRecord.setId(
                "https://example.org/catalog_records/transit-2024");
        transitRecord.setTitle(
                "Catalog registration: transit schedule 2024");
        transitRecord.setPrimaryTopic(URI.create(
                "https://example.org/datasets/transit-2024"));
        transitRecord.setListingDate(
                OffsetDateTime.parse("2024-01-05T00:00:00Z"));
        transitRecord.setModificationDate(OffsetDateTime.now());
        catalogRecords.put(transitRecord);
    }
}
