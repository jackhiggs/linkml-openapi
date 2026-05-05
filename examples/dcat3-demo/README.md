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
during the `generate-sources` phase (with `--path-prefix /api/v1`),
compiles, and boots Spring Boot on port 8080. To use a different
port:

```bash
mvn spring-boot:run -Dspring-boot.run.arguments=--server.port=8089
```

> **Path prefix.** The demo serves every endpoint under `/api/v1`
> (`/api/v1/catalogs/{id}`, `/api/v1/datasets`, …). The Maven build
> passes `--path-prefix /api/v1` to `gen-spring-server`, which emits
> a class-level `@RequestMapping("/api/v1")` on every controller
> interface and prefixes every `paths:` key in the sidecar
> `resources/openapi.yaml` so springdoc's runtime view matches.
> Drop the `--path-prefix` argument from `pom.xml` to serve at the
> root again.

## URLs

| Path | Purpose |
|---|---|
| `http://localhost:8080/swagger-ui/index.html` | Interactive Swagger UI |
| `http://localhost:8080/redoc.html` | ReDoc rendering |
| `http://localhost:8080/v3/api-docs` | Live OpenAPI 3.1 spec (springdoc) |

(Swagger UI / ReDoc / `/v3/api-docs` are springdoc's own paths and
are not affected by the resource path prefix.)

## Content negotiation

The demo seeds an in-memory store with a `Catalog`, two `Dataset`s
(one a `DatasetSeries`), one `DataService`, and a couple of
`Distribution`s. Every endpoint serves JSON by default; pass an
`Accept` header to switch wire format:

```bash
# JSON (default)
curl -s http://localhost:8080/api/v1/catalogs/city-data

# Turtle
curl -s -H 'Accept: text/turtle' http://localhost:8080/api/v1/catalogs/city-data

# JSON-LD
curl -s -H 'Accept: application/ld+json' http://localhost:8080/api/v1/catalogs/city-data

# RDF/XML
curl -s -H 'Accept: application/rdf+xml' http://localhost:8080/api/v1/catalogs/city-data
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

After the kebab-case transform, inherited-slot suppression, and
`/api/v1` path prefix, the demo exposes ~54 paths:

- **Top-level CRUD** on every resource class:
  `/api/v1/catalogs`, `/api/v1/datasets`, `/api/v1/dataset-series`,
  `/api/v1/data-services`, `/api/v1/agents`, `/api/v1/catalog-records`
- **Single-level nested**:
  `/api/v1/datasets/{id}/distribution`,
  `/api/v1/catalogs/{id}/dataset`,
  `/api/v1/data-services/{id}/serves-dataset`,
  `/api/v1/<resource>/{id}/contact-point|contributor|creator`
- **Deep-chained item paths**:
  `/api/v1/catalogs/{cat}/dataset/{ds}/in-series/{id}`,
  `/api/v1/catalogs/{cat}/dataset/{ds}/distribution/{id}` (Distribution
  is `nested_only` — no flat `/api/v1/distributions` URL)
- **Slot-driven query parameters** on every list endpoint:
  `?title=`, `?issued__gte=`, `?issued__lte=`, `?modified__gte=`,
  `?sort=` array, plus `?limit=` and `?offset=` for pagination

## Try the deep chain

```bash
curl -s "http://localhost:8080/api/v1/catalogs/city-data/dataset/transit-2024/distribution/transit-2024-gtfs"
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
