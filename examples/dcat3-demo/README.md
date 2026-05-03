# dcat3-demo

End-to-end demonstration of the LinkML → OpenAPI → Spring → RDF
pipeline against the W3C DCAT-3 vocabulary.

The Spring service generates its DTOs and controller interfaces from
[`tests/fixtures/dcat3.yaml`](../../tests/fixtures/dcat3.yaml) at
build time, boots Spring Boot with springdoc, and serves the same
controllers under JSON, Turtle, JSON-LD, and RDF/XML — no per-class
marshaling code. Auto-RDF marshaling comes from
[`spring-rdf-runtime`](../spring-rdf-runtime/), which reads the
`x-rdf-class` / `x-rdf-property` extensions from the sidecar OpenAPI
spec and dispatches via Apache Jena.

## Prerequisites

- Java 21+ (the demo was tested with Java 25 from `brew install
  openjdk`; any JDK 21+ works)
- Maven 3.9+
- [`uv`](https://docs.astral.sh/uv/) for the LinkML→Spring code
  generation step
- Python 3.11+ (handled by `uv`)

## Running

From the repo root:

```bash
cd examples/dcat3-demo
JAVA_HOME=/opt/homebrew/opt/openjdk/libexec/openjdk.jdk/Contents/Home \
  mvn -B spring-boot:run
```

Maven runs `gen-spring-server` against `tests/fixtures/dcat3.yaml`
during the `generate-sources` phase, compiles, and boots Spring Boot
on port 8080. To use a different port:

```bash
mvn spring-boot:run -Dspring-boot.run.arguments=--server.port=8089
```

## URLs

| Path | Purpose |
|---|---|
| `http://localhost:8080/swagger-ui/index.html` | Interactive Swagger UI |
| `http://localhost:8080/redoc.html` | ReDoc rendering |
| `http://localhost:8080/v3/api-docs` | Live OpenAPI 3.1 spec (springdoc) |

## Content negotiation

The demo seeds an in-memory store with a `Catalog`, two `Dataset`s
(one a `DatasetSeries`), one `DataService`, and a couple of
`Distribution`s. Every endpoint serves JSON by default; pass an
`Accept` header to switch wire format:

```bash
# JSON (default)
curl -s http://localhost:8080/catalogs/city-data

# Turtle
curl -s -H 'Accept: text/turtle' http://localhost:8080/catalogs/city-data

# JSON-LD
curl -s -H 'Accept: application/ld+json' http://localhost:8080/catalogs/city-data

# RDF/XML
curl -s -H 'Accept: application/rdf+xml' http://localhost:8080/catalogs/city-data
```

Sample Turtle output:

```turtle
<https://example.org/catalogs/city-data>
        a       <http://www.w3.org/ns/dcat#Catalog>;
        <http://purl.org/dc/terms/title>     "City Open Data Portal";
        <http://purl.org/dc/terms/publisher> <https://example.org/agents/city-data-office>;
        <http://www.w3.org/ns/dcat#dataset>
                <https://example.org/datasets/transit-2024> ,
                <https://example.org/datasets/budget-2024>;
        <http://www.w3.org/ns/dcat#service>
                <https://example.org/data_services/transit-api>;
        <http://www.w3.org/ns/dcat#keyword>
                "government" , "civic" , "open-data" ;
        <http://xmlns.com/foaf/0.1/homepage> <https://city.example.org/data> .
```

The same DTO; four wire formats; class IRIs and slot predicates flow
straight from the LinkML schema's `class_uri` / `slot_uri` declarations
through to the RDF output.

## URL surface

After the kebab-case transform and inherited-slot suppression, the
demo exposes ~54 paths:

- **Top-level CRUD** on every resource class:
  `/catalogs`, `/datasets`, `/dataset-series`, `/data-services`,
  `/agents`, `/catalog-records`
- **Single-level nested**:
  `/datasets/{id}/distribution`, `/catalogs/{id}/dataset`,
  `/data-services/{id}/serves-dataset`,
  `/<resource>/{id}/contact-point|contributor|creator`
- **Deep-chained item paths**:
  `/catalogs/{cat}/dataset/{ds}/in-series/{id}`,
  `/catalogs/{cat}/dataset/{ds}/distribution/{id}` (Distribution is
  `nested_only` — no flat `/distributions` URL)
- **Slot-driven query parameters** on every list endpoint:
  `?title=`, `?issued__gte=`, `?issued__lte=`, `?modified__gte=`,
  `?sort=` array, plus `?limit=` and `?offset=` for pagination

## Try the deep chain

```bash
curl -s "http://localhost:8080/catalogs/city-data/dataset/transit-2024/distribution/transit-2024-gtfs"
```

Returns the seeded `Distribution` with `title`, `mediaType`, `byteSize`,
proper IRI references for `accessURL` / `downloadURL`, and the
discriminator pinned (`resourceType: "Distribution"` plus the
`#type` legacy back-compat marker).

## How it's wired

```
tests/fixtures/dcat3.yaml          ← LinkML schema (single source of truth)
        │
        │ gen-spring-server (Maven generate-sources)
        ▼
target/generated-sources/openapi/
   ├── java/io/example/dcat/model/*.java   ← DTOs with Jackson polymorphism
   ├── java/io/example/dcat/api/*Api.java  ← Controller interfaces
   └── resources/openapi.yaml              ← Sidecar spec with x-rdf-* extensions
        │
        │ Spring Boot picks up the controllers
        │ spring-rdf-runtime auto-registers HttpMessageConverter
        │ from the sidecar's x-rdf-class / x-rdf-property
        ▼
Live service: JSON + Turtle + JSON-LD + RDF/XML on the same controllers
```

Hand-written code under `src/main/java`:
- `Application.java` — Spring Boot entry point
- `StubControllers.java` — `@RestController` beans that delegate to
  `Stores` for in-memory CRUD
- `store/InMemoryStore.java`, `store/Stores.java` — in-memory map of
  IRI → DTO with seed data

The generated DTOs and controller interfaces live under
`target/generated-sources/openapi/` (gitignored, regenerated every
build).
