package io.linkmlspring.rdf;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.time.OffsetDateTime;
import java.util.List;
import org.apache.jena.rdf.model.Model;
import org.apache.jena.rdf.model.Resource;
import org.apache.jena.rdf.model.Statement;
import org.apache.jena.rdf.model.StmtIterator;
import org.apache.jena.vocabulary.RDF;
import org.junit.jupiter.api.Test;

/**
 * Reflection-based DTO → Jena Model mapping. Uses a tiny in-test DTO
 * (no dependency on the demo project's Catalog) so the tests pin the
 * mapping behaviour in isolation.
 */
class RdfMapperTest {

    static class Catalog {
        public String id;
        public String title;
        public URI landingPage;
        public List<String> keyword;
        public OffsetDateTime issued;
        public Long byteSize;
        // Synthesised polymorphism markers — must be skipped.
        public String resourceType;
        public String legacyType;
    }

    private static SpecRegistry registry() {
        return SpecRegistry.load("/test-spec.yaml");
    }

    private static Resource buildSubject(Catalog c) {
        Model m = new RdfMapper(registry()).toModel(c);
        return m.listSubjects().nextResource();
    }

    @Test
    void idFieldBecomesSubjectIri() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        Resource subj = buildSubject(c);
        assertEquals("https://example.org/catalogs/cat-1", subj.getURI());
    }

    @Test
    void rdfTypeEmittedFromXRdfClass() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        Model m = new RdfMapper(registry()).toModel(c);
        boolean hasType = m.contains(
                m.createResource("https://example.org/catalogs/cat-1"),
                RDF.type,
                m.createResource("http://www.w3.org/ns/dcat#Catalog"));
        assertTrue(hasType);
    }

    @Test
    void stringFieldBecomesPlainLiteralUnderItsPredicate() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        c.title = "Open Data Portal";
        Model m = new RdfMapper(registry()).toModel(c);
        Statement s = m.listStatements(
                        m.createResource("https://example.org/catalogs/cat-1"),
                        m.createProperty("http://purl.org/dc/terms/title"),
                        (org.apache.jena.rdf.model.RDFNode) null)
                .nextStatement();
        assertEquals("Open Data Portal", s.getString());
    }

    @Test
    void uriFieldBecomesRdfResource() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        c.landingPage = URI.create("https://example.org/catalog/cat-1");
        Model m = new RdfMapper(registry()).toModel(c);
        Statement s = m.listStatements(
                        null,
                        m.createProperty(
                                "http://www.w3.org/ns/dcat#landingPage"),
                        (org.apache.jena.rdf.model.RDFNode) null)
                .nextStatement();
        assertTrue(s.getObject().isURIResource());
        assertEquals(
                "https://example.org/catalog/cat-1",
                s.getObject().asResource().getURI());
    }

    @Test
    void multivaluedListBecomesMultipleTriples() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        c.keyword = List.of("a", "b", "c");
        Model m = new RdfMapper(registry()).toModel(c);
        StmtIterator it = m.listStatements(
                null,
                m.createProperty("http://www.w3.org/ns/dcat#keyword"),
                (org.apache.jena.rdf.model.RDFNode) null);
        int count = 0;
        while (it.hasNext()) {
            it.nextStatement();
            count++;
        }
        assertEquals(3, count);
    }

    static class Doc {
        public String id;
        public OffsetDateTime issued;
        public Long byteSize;
    }

    @Test
    void offsetDateTimeBecomesXsdDateTimeLiteral() {
        // OffsetDateTime → typed literal so RDF consumers preserve
        // the type rather than re-parsing strings.
        Doc d = new Doc();
        d.id = "https://example.org/d/1";
        d.issued = OffsetDateTime.parse("2024-01-15T00:00:00Z");
        Model m = new RdfMapper(registry()).toModel(d);
        Statement s = m.listStatements(
                        null,
                        m.createProperty("http://purl.org/dc/terms/issued"),
                        (org.apache.jena.rdf.model.RDFNode) null)
                .nextStatement();
        assertTrue(s.getLiteral().getDatatypeURI().endsWith("dateTime"));
    }

    @Test
    void numberFieldBecomesXsdIntegerLiteral() {
        Doc d = new Doc();
        d.id = "https://example.org/d/1";
        d.byteSize = 4096L;
        Model m = new RdfMapper(registry()).toModel(d);
        Statement s = m.listStatements(
                        null,
                        m.createProperty("http://www.w3.org/ns/dcat#byteSize"),
                        (org.apache.jena.rdf.model.RDFNode) null)
                .nextStatement();
        assertTrue(s.getLiteral().getDatatypeURI().endsWith("integer"));
        assertEquals(4096L, s.getLong());
    }

    @Test
    void resourceTypeAndLegacyTypeFieldsAreSkipped() {
        // These are JSON polymorphism markers, not RDF predicates.
        // rdf:type comes from x-rdf-class; the field values would
        // duplicate it (with a string label rather than a class IRI).
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        c.resourceType = "Catalog";
        c.legacyType = "com.xyz.dcat.Catalog";
        Model m = new RdfMapper(registry()).toModel(c);
        // Single rdf:type triple — only the one from x-rdf-class.
        StmtIterator it = m.listStatements(
                null,
                RDF.type,
                (org.apache.jena.rdf.model.RDFNode) null);
        int count = 0;
        while (it.hasNext()) {
            it.nextStatement();
            count++;
        }
        assertEquals(1, count);
    }

    @Test
    void readsBackInstanceFromTurtle() {
        // Round-trip a full DTO: serialise → parse → reconstruct.
        // Confirms that the spec-driven mapping works in both
        // directions against the same SpecRegistry.
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        c.title = "Open Data Portal";
        c.landingPage = URI.create(
                "https://example.org/catalog/cat-1");
        c.keyword = List.of("open-data", "civic");
        Model written = new RdfMapper(registry()).toModel(c);

        // Serialise + reparse to mimic the HttpMessageConverter
        // round-trip (write side then read side).
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        org.apache.jena.riot.RDFDataMgr.write(
                out, written, org.apache.jena.riot.RDFFormat.TURTLE);
        Model parsed = org.apache.jena.rdf.model.ModelFactory
                .createDefaultModel();
        org.apache.jena.riot.RDFParser
                .source(new java.io.ByteArrayInputStream(out.toByteArray()))
                .lang(org.apache.jena.riot.Lang.TURTLE)
                .parse(parsed);

        Catalog hydrated = new RdfMapper(registry())
                .fromModel(parsed, Catalog.class);
        assertEquals(
                "https://example.org/catalogs/cat-1", hydrated.id);
        assertEquals("Open Data Portal", hydrated.title);
        assertEquals(
                URI.create("https://example.org/catalog/cat-1"),
                hydrated.landingPage);
        assertEquals(2, hydrated.keyword.size());
        assertTrue(hydrated.keyword.contains("open-data"));
        assertTrue(hydrated.keyword.contains("civic"));
    }

    @Test
    void readsReturnsNullWhenNoSubjectMatchesType() {
        // Empty model — no subject carries the expected rdf:type, so
        // fromModel returns null rather than fabricating an instance.
        Model empty = org.apache.jena.rdf.model.ModelFactory
                .createDefaultModel();
        Catalog dto = new RdfMapper(registry())
                .fromModel(empty, Catalog.class);
        org.junit.jupiter.api.Assertions.assertNull(dto);
    }

    @Test
    void nullFieldsEmitNoTriples() {
        Catalog c = new Catalog();
        c.id = "https://example.org/catalogs/cat-1";
        // title, landingPage, etc. are all null.
        Model m = new RdfMapper(registry()).toModel(c);
        // Only the rdf:type triple should be present.
        assertEquals(1, m.size());
        assertFalse(m.contains(
                null,
                m.createProperty("http://purl.org/dc/terms/title")));
    }

}
