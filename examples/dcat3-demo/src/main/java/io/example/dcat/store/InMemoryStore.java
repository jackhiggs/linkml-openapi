package io.example.dcat.store;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Function;

/**
 * Tiny per-class in-memory CRUD store for the demo. Not production —
 * no persistence, no transactions, no concurrency guarantees beyond
 * {@link ConcurrentHashMap} atomicity. Lets the controllers serve
 * real DCAT-3 data so the spec-driven RDF round-trip can be observed
 * end-to-end (POST a Turtle Catalog, GET it back as JSON-LD).
 *
 * <p>Each {@code InMemoryStore<T>} is keyed by the resource's IRI
 * (the value of the DTO's {@code id} field). The {@code idGetter}
 * lambda extracts that key — Java DTOs from the linkml-spring
 * generator name the field {@code id} consistently, but using a
 * lambda keeps the store reusable for any DTO shape.
 */
public class InMemoryStore<T> {

    private final Map<String, T> byId = new ConcurrentHashMap<>();
    private final Function<T, String> idGetter;

    public InMemoryStore(Function<T, String> idGetter) {
        this.idGetter = idGetter;
    }

    public List<T> list(int limit, int offset) {
        // Stable ordering by IRI so paging is deterministic across
        // requests. Real services would page over a sorted index.
        List<T> all = new ArrayList<>(byId.values());
        all.sort((a, b) -> idGetter.apply(a).compareTo(idGetter.apply(b)));
        int start = Math.min(Math.max(offset, 0), all.size());
        int end = Math.min(start + Math.max(limit, 0), all.size());
        return all.subList(start, end);
    }

    public Optional<T> get(String id) {
        return Optional.ofNullable(byId.get(id));
    }

    public T put(T item) {
        byId.put(idGetter.apply(item), item);
        return item;
    }

    public boolean remove(String id) {
        return byId.remove(id) != null;
    }

    public int size() {
        return byId.size();
    }
}
