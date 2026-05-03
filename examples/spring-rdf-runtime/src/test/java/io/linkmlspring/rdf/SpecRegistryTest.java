package io.linkmlspring.rdf;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.Map;
import org.junit.jupiter.api.Test;

class SpecRegistryTest {

    @Test
    void loadsXRdfClassFromSpec() {
        SpecRegistry r = SpecRegistry.load("/test-spec.yaml");
        assertEquals(
                "http://www.w3.org/ns/dcat#Catalog",
                r.classIri("Catalog"));
    }

    @Test
    void schemaWithoutXRdfClassReturnsNull() {
        // ``NoRdf`` schema deliberately lacks the extension — confirms
        // we silently skip schemas with no RDF metadata rather than
        // throwing or registering bogus mappings.
        SpecRegistry r = SpecRegistry.load("/test-spec.yaml");
        assertNull(r.classIri("NoRdf"));
    }

    @Test
    void loadsXRdfPropertyForFieldsOnSchema() {
        SpecRegistry r = SpecRegistry.load("/test-spec.yaml");
        Map<String, String> props = r.propertiesFor("Catalog");
        assertEquals(
                "http://purl.org/dc/terms/title", props.get("title"));
        assertEquals(
                "http://www.w3.org/ns/dcat#landingPage",
                props.get("landingPage"));
        assertEquals(
                "http://www.w3.org/ns/dcat#keyword",
                props.get("keyword"));
    }

    @Test
    void unknownSchemaReturnsEmptyPropertyMap() {
        // Defensive: callers walking unknown classes shouldn't crash.
        SpecRegistry r = SpecRegistry.load("/test-spec.yaml");
        assertTrue(r.propertiesFor("Nonexistent").isEmpty());
    }

    @Test
    void missingClasspathSpecRaisesIllegalState() {
        // Loud failure when the spec is misconfigured — silently
        // serving an empty registry would mean every RDF response
        // serialises to an empty graph with no diagnostic.
        assertThrows(
                IllegalStateException.class,
                () -> SpecRegistry.load("/no-such-spec.yaml"));
    }
}
