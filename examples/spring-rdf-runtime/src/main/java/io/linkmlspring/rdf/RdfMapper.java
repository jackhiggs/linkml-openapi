package io.linkmlspring.rdf;

import java.lang.reflect.Field;
import java.lang.reflect.ParameterizedType;
import java.lang.reflect.Type;
import java.net.URI;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.apache.jena.datatypes.xsd.XSDDatatype;
import org.apache.jena.rdf.model.Literal;
import org.apache.jena.rdf.model.Model;
import org.apache.jena.rdf.model.ModelFactory;
import org.apache.jena.rdf.model.Property;
import org.apache.jena.rdf.model.RDFNode;
import org.apache.jena.rdf.model.Resource;
import org.apache.jena.rdf.model.Statement;
import org.apache.jena.rdf.model.StmtIterator;
import org.apache.jena.vocabulary.RDF;

/**
 * Reflection-based DTO → Jena Model mapper, driven by a
 * {@link SpecRegistry} loaded from the OpenAPI spec at startup.
 *
 * <p>Conventions:
 *
 * <ul>
 *   <li>The DTO's {@code id} field provides the RDF subject IRI. If
 *       absent or null, the resource is emitted as a blank node.
 *   <li>Inherited fields are walked too — Catalog reaches every
 *       Resource/Dataset field via {@link Class#getSuperclass()}.
 *   <li>Embedded value classes (no {@code id} field) become blank
 *       nodes attached to the parent via the predicate from the
 *       parent's spec entry.
 *   <li>{@code java.net.URI} field values become RDF resources;
 *       primitives become typed literals; {@link OffsetDateTime}
 *       becomes an {@code xsd:dateTime} literal; everything else
 *       becomes a plain literal via {@code toString}.
 * </ul>
 */
public final class RdfMapper {

    /** DTO field that carries the subject IRI — structural marker,
     *  not a data triple, so it doesn't come from the spec. If the
     *  codegen ever picks a different name, surface it via an
     *  ``x-rdf-id-field`` class extension and read it from the
     *  registry; for now the convention is enough. */
    private static final String FIELD_ID = "id";

    private final SpecRegistry registry;

    public RdfMapper(SpecRegistry registry) {
        this.registry = registry;
    }

    /** Add the DTO's triples to the supplied model and return the
     *  subject resource (so callers can chain with related DTOs). */
    public Resource addToModel(Object dto, Model model) {
        if (dto == null) {
            return null;
        }
        String schemaName = dto.getClass().getSimpleName();
        Resource subject = subjectFor(dto, model);
        String classIri = registry.classIri(schemaName);
        if (classIri != null) {
            subject.addProperty(RDF.type, model.createResource(classIri));
        }
        // Walk inherited fields too: Catalog ⇢ Dataset ⇢ Resource.
        for (Class<?> c = dto.getClass(); c != null && c != Object.class;
                c = c.getSuperclass()) {
            for (Field f : c.getDeclaredFields()) {
                emitField(dto, c, f, subject, model);
            }
        }
        return subject;
    }

    /** Build a fresh Model carrying the single DTO. Used by the
     *  HttpMessageConverter on each write. */
    public Model toModel(Object dto) {
        Model model = ModelFactory.createDefaultModel();
        addToModel(dto, model);
        return model;
    }

    // --- internals ----------------------------------------------------

    private Resource subjectFor(Object dto, Model model) {
        try {
            Field idField = findField(dto.getClass(), FIELD_ID);
            if (idField != null) {
                idField.setAccessible(true);
                Object id = idField.get(dto);
                if (id != null) {
                    return model.createResource(id.toString());
                }
            }
        } catch (IllegalAccessException ignored) {
            // fall through to blank node
        }
        return model.createResource(); // blank node
    }

    private void emitField(
            Object dto,
            Class<?> declaringClass,
            Field f,
            Resource subject,
            Model model) {
        // The id field is the subject IRI itself, not a triple.
        if (FIELD_ID.equals(f.getName())) {
            return;
        }
        // Static / synthetic fields aren't user data.
        int mods = f.getModifiers();
        if (java.lang.reflect.Modifier.isStatic(mods)) {
            return;
        }
        String predicateIri = registry.propertyIri(
                declaringClass.getSimpleName(), f.getName());
        if (predicateIri == null) {
            // Walk up: the predicate may be declared on a superclass's
            // schema entry rather than the leaf's.
            for (Class<?> c = declaringClass.getSuperclass();
                    c != null && c != Object.class;
                    c = c.getSuperclass()) {
                predicateIri = registry.propertyIri(
                        c.getSimpleName(), f.getName());
                if (predicateIri != null) {
                    break;
                }
            }
        }
        if (predicateIri == null) {
            return; // no RDF mapping declared — skip silently
        }

        try {
            f.setAccessible(true);
            Object value = f.get(dto);
            if (value == null) {
                return;
            }
            Property predicate = model.createProperty(predicateIri);
            if (value instanceof Collection<?> col) {
                for (Object item : col) {
                    addObjectTriple(subject, predicate, item, model);
                }
            } else {
                addObjectTriple(subject, predicate, value, model);
            }
        } catch (IllegalAccessException ignored) {
            // Skip inaccessible fields rather than crash the marshal.
        }
    }

    private void addObjectTriple(
            Resource subject, Property predicate, Object value, Model model) {
        if (value == null) {
            return;
        }
        RDFNode node = toRdfNode(value, model);
        if (node != null) {
            subject.addProperty(predicate, node);
        }
    }

    private RDFNode toRdfNode(Object value, Model model) {
        if (value instanceof URI uri) {
            return model.createResource(uri.toString());
        }
        if (value instanceof OffsetDateTime odt) {
            return model.createTypedLiteral(
                    odt.toString(), XSDDatatype.XSDdateTime);
        }
        if (value instanceof Number n) {
            // Numbers go in as xsd-typed literals so RDF consumers
            // round-trip the type rather than re-parsing strings.
            if (value instanceof Integer || value instanceof Long) {
                return model.createTypedLiteral(
                        value.toString(), XSDDatatype.XSDinteger);
            }
            if (value instanceof Float || value instanceof Double) {
                return model.createTypedLiteral(
                        value.toString(), XSDDatatype.XSDdouble);
            }
            return model.createTypedLiteral(
                    n.toString(), XSDDatatype.XSDdecimal);
        }
        if (value instanceof Boolean b) {
            return model.createTypedLiteral(
                    b.toString(), XSDDatatype.XSDboolean);
        }
        if (value instanceof String s) {
            return model.createLiteral(s);
        }
        // Anything else — assume it's a known DTO whose schema we
        // recognise: recurse, attach as a sub-resource.
        if (registry.classIri(value.getClass().getSimpleName()) != null) {
            return addToModel(value, model);
        }
        // Fallback: stringify so we don't drop the value silently.
        return model.createLiteral(value.toString());
    }

    private static Field findField(Class<?> cls, String name) {
        for (Class<?> c = cls; c != null && c != Object.class;
                c = c.getSuperclass()) {
            try {
                return c.getDeclaredField(name);
            } catch (NoSuchFieldException ignored) {
                // try parent
            }
        }
        return null;
    }

    // =================================================================
    // Reverse direction: Jena Model → Java DTO
    // =================================================================

    /**
     * Hydrate an instance of {@code targetClass} from {@code model} by
     * locating a subject whose ``rdf:type`` matches the class's
     * ``x-rdf-class`` (looked up via the registry), then mapping each
     * predicate triple to the DTO's matching field.
     *
     * <p>Conventions mirror {@link #addToModel}:
     * <ul>
     *   <li>Subject IRI → ``id`` field
     *   <li>Predicate ↔ field by reverse lookup of
     *       {@link SpecRegistry#propertiesFor}
     *   <li>RDF resource → {@link URI}; literal → primitive / String /
     *       OffsetDateTime per field type; multivalued field → all
     *       triples for the predicate gathered into a List
     * </ul>
     *
     * <p>Returns {@code null} when no subject in the model carries the
     * expected type — callers raise as appropriate.
     */
    public <T> T fromModel(Model model, Class<T> targetClass) {
        String classIri = registry.classIri(targetClass.getSimpleName());
        if (classIri == null) {
            throw new IllegalStateException(
                    "Class " + targetClass.getSimpleName()
                            + " has no x-rdf-class in the loaded spec");
        }
        Resource typeResource = model.createResource(classIri);
        StmtIterator subjects = model.listStatements(
                null, RDF.type, typeResource);
        if (!subjects.hasNext()) {
            return null;
        }
        Resource subject = subjects.nextStatement().getSubject();
        try {
            T instance = targetClass.getDeclaredConstructor()
                    .newInstance();
            hydrate(instance, subject, model);
            return instance;
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException(
                    "Failed to instantiate " + targetClass.getName(),
                    e);
        }
    }

    private void hydrate(Object instance, Resource subject, Model model)
            throws ReflectiveOperationException {
        // 1. Set the id field to the subject IRI (or leave blank if a
        //    blank node — we serialise blank nodes for embedded
        //    objects only).
        if (subject.isURIResource()) {
            Field idField = findField(instance.getClass(), FIELD_ID);
            if (idField != null) {
                idField.setAccessible(true);
                idField.set(instance, coerceToFieldType(
                        idField.getGenericType(), subject.getURI()));
            }
        }

        // 2. Build a reverse predicate→field map by walking the DTO's
        //    inheritance chain and consulting the registry. Cached
        //    nowhere — fresh per call; fast enough for typical
        //    payloads, and avoids stale state if the registry is
        //    swapped at runtime.
        Map<String, Field> predicateToField = new HashMap<>();
        for (Class<?> c = instance.getClass();
                c != null && c != Object.class;
                c = c.getSuperclass()) {
            for (Field f : c.getDeclaredFields()) {
                if (FIELD_ID.equals(f.getName())) {
                    continue;
                }
                String predicate = lookupPredicate(c, f.getName());
                if (predicate != null) {
                    predicateToField.putIfAbsent(predicate, f);
                }
            }
        }

        // 3. Walk every triple from the subject and populate fields.
        Map<Field, List<Object>> multivaluedAccum = new HashMap<>();
        StmtIterator it = subject.listProperties();
        while (it.hasNext()) {
            Statement s = it.nextStatement();
            Property p = s.getPredicate();
            if (RDF.type.equals(p)) {
                continue; // handled by class dispatch, not as data
            }
            Field f = predicateToField.get(p.getURI());
            if (f == null) {
                continue;
            }
            f.setAccessible(true);
            Object value = rdfNodeToJava(
                    s.getObject(), elementType(f), model);
            if (value == null) {
                continue;
            }
            if (Collection.class.isAssignableFrom(f.getType())) {
                multivaluedAccum
                        .computeIfAbsent(f, k -> new ArrayList<>())
                        .add(value);
            } else {
                f.set(instance, value);
            }
        }
        for (Map.Entry<Field, List<Object>> e : multivaluedAccum.entrySet()) {
            e.getKey().set(instance, e.getValue());
        }
    }

    private String lookupPredicate(Class<?> cls, String fieldName) {
        // The registry is keyed by schema name (Java class simple
        // name); inherited fields' predicates may be on an ancestor.
        for (Class<?> c = cls; c != null && c != Object.class;
                c = c.getSuperclass()) {
            String iri = registry.propertyIri(c.getSimpleName(), fieldName);
            if (iri != null) {
                return iri;
            }
        }
        return null;
    }

    private static Type elementType(Field f) {
        // For ``List<X>`` fields we want X to drive the per-element
        // coercion. Otherwise the field's own type.
        if (Collection.class.isAssignableFrom(f.getType())) {
            Type generic = f.getGenericType();
            if (generic instanceof ParameterizedType pt
                    && pt.getActualTypeArguments().length == 1) {
                return pt.getActualTypeArguments()[0];
            }
            return Object.class;
        }
        return f.getGenericType();
    }

    private Object rdfNodeToJava(RDFNode node, Type targetType, Model model) {
        Class<?> raw = rawClass(targetType);
        if (node.isURIResource()) {
            String uri = node.asResource().getURI();
            if (URI.class.equals(raw)) {
                return URI.create(uri);
            }
            if (String.class.equals(raw)) {
                return uri;
            }
            // Embedded sub-resource — recurse if we know the type.
            if (raw != null
                    && registry.classIri(raw.getSimpleName()) != null) {
                try {
                    Object child = raw.getDeclaredConstructor().newInstance();
                    hydrate(child, node.asResource(), model);
                    return child;
                } catch (ReflectiveOperationException e) {
                    return null;
                }
            }
            return uri;
        }
        if (node.isAnon()) {
            // Blank node — embedded structured value.
            if (raw != null
                    && registry.classIri(raw.getSimpleName()) != null) {
                try {
                    Object child = raw.getDeclaredConstructor().newInstance();
                    hydrate(child, node.asResource(), model);
                    return child;
                } catch (ReflectiveOperationException e) {
                    return null;
                }
            }
            return null;
        }
        if (node.isLiteral()) {
            Literal lit = node.asLiteral();
            return coerceLiteral(lit, raw);
        }
        return null;
    }

    private static Class<?> rawClass(Type t) {
        if (t instanceof Class<?> c) {
            return c;
        }
        if (t instanceof ParameterizedType pt
                && pt.getRawType() instanceof Class<?> rc) {
            return rc;
        }
        return Object.class;
    }

    private static Object coerceLiteral(Literal lit, Class<?> target) {
        if (target == null || String.class.equals(target)) {
            return lit.getString();
        }
        if (Long.class.equals(target) || long.class.equals(target)) {
            return lit.getLong();
        }
        if (Integer.class.equals(target) || int.class.equals(target)) {
            return lit.getInt();
        }
        if (Double.class.equals(target) || double.class.equals(target)) {
            return lit.getDouble();
        }
        if (Float.class.equals(target) || float.class.equals(target)) {
            return lit.getFloat();
        }
        if (Boolean.class.equals(target) || boolean.class.equals(target)) {
            return lit.getBoolean();
        }
        if (OffsetDateTime.class.equals(target)) {
            return OffsetDateTime.parse(lit.getString());
        }
        if (URI.class.equals(target)) {
            return URI.create(lit.getString());
        }
        return lit.getString();
    }

    private static Object coerceToFieldType(Type targetType, String value) {
        Class<?> raw = rawClass(targetType);
        if (URI.class.equals(raw)) {
            return URI.create(value);
        }
        return value;
    }
}
