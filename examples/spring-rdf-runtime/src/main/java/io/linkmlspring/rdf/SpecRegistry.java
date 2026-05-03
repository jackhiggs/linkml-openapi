package io.linkmlspring.rdf;

import java.io.InputStream;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.yaml.snakeyaml.Yaml;

/**
 * Loads the OpenAPI spec from the classpath at startup and exposes
 * the RDF identity map: schema name → x-rdf-class IRI, plus
 * per-schema property name → x-rdf-property IRI.
 *
 * <p>The spec is the single source of truth — no parallel Java
 * annotations, no codegen-time mapping table. Adding a new RDF-
 * carrying field on the LinkML side is automatically picked up
 * by the runtime when the spec regenerates.
 *
 * <p>Walks both top-level {@code components/schemas/<name>/properties}
 * and the {@code allOf[].properties} branch used by the inheritance
 * shape, so subclass schemas pick up their own local properties (the
 * inherited ones come from the parent class's entry).
 */
public final class SpecRegistry {

    /**
     * @param specClasspath classpath path of the OpenAPI YAML spec,
     *                      typically {@code "/openapi.yaml"}.
     */
    @SuppressWarnings("unchecked")
    public static SpecRegistry load(String specClasspath) {
        try (InputStream in =
                SpecRegistry.class.getResourceAsStream(specClasspath)) {
            if (in == null) {
                throw new IllegalStateException(
                        "OpenAPI spec not found on classpath at "
                                + specClasspath);
            }
            Map<String, Object> root = new Yaml().load(in);
            Map<String, Object> schemas = path(
                    root, "components", "schemas");
            return new SpecRegistry(schemas == null
                    ? Collections.emptyMap()
                    : schemas);
        } catch (Exception e) {
            throw new IllegalStateException(
                    "Failed to load OpenAPI spec from classpath:"
                            + specClasspath,
                    e);
        }
    }

    private final Map<String, String> classIri;
    private final Map<String, Map<String, String>> propertyIri;

    @SuppressWarnings("unchecked")
    private SpecRegistry(Map<String, Object> schemas) {
        this.classIri = new HashMap<>();
        this.propertyIri = new HashMap<>();
        for (Map.Entry<String, Object> entry : schemas.entrySet()) {
            String schemaName = entry.getKey();
            if (!(entry.getValue() instanceof Map<?, ?> sch)) {
                continue;
            }
            Map<String, Object> schema = (Map<String, Object>) sch;
            Object cls = schema.get("x-rdf-class");
            if (cls instanceof String s) {
                this.classIri.put(schemaName, s);
            }
            Map<String, String> propsOut = new LinkedHashMap<>();
            collectProperties(schema, propsOut);
            if (!propsOut.isEmpty()) {
                this.propertyIri.put(schemaName, propsOut);
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static void collectProperties(
            Map<String, Object> schema, Map<String, String> out) {
        if (schema.get("properties") instanceof Map<?, ?> props) {
            walk((Map<String, Object>) props, out);
        }
        if (schema.get("allOf") instanceof List<?> allOf) {
            for (Object part : allOf) {
                if (part instanceof Map<?, ?> p
                        && p.get("properties") instanceof Map<?, ?> pp) {
                    walk((Map<String, Object>) pp, out);
                }
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static void walk(
            Map<String, Object> props, Map<String, String> out) {
        for (Map.Entry<String, Object> e : props.entrySet()) {
            if (!(e.getValue() instanceof Map<?, ?> v)) {
                continue;
            }
            Object iri = ((Map<String, Object>) v).get("x-rdf-property");
            if (iri instanceof String s) {
                out.put(e.getKey(), s);
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> path(
            Map<String, Object> root, String... segments) {
        Map<String, Object> cur = root;
        for (String seg : segments) {
            Object next = cur == null ? null : cur.get(seg);
            cur = (next instanceof Map) ? (Map<String, Object>) next : null;
        }
        return cur;
    }

    /** RDF type IRI for a Java/spec class name, or {@code null}. */
    public String classIri(String schemaName) {
        return classIri.get(schemaName);
    }

    /** RDF predicate IRI for a property on a class, or {@code null}. */
    public String propertyIri(String schemaName, String propertyName) {
        Map<String, String> props = propertyIri.get(schemaName);
        return props == null ? null : props.get(propertyName);
    }

    /** Property → predicate map for a class (empty when missing). */
    public Map<String, String> propertiesFor(String schemaName) {
        return propertyIri.getOrDefault(
                schemaName, Collections.emptyMap());
    }
}
