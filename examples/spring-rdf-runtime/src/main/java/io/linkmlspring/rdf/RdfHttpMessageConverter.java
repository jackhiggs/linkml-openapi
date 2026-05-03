package io.linkmlspring.rdf;

import java.io.IOException;
import java.util.List;
import org.apache.jena.rdf.model.Model;
import org.apache.jena.riot.RDFFormat;
import org.springframework.http.HttpInputMessage;
import org.springframework.http.HttpOutputMessage;
import org.springframework.http.MediaType;
import org.springframework.http.converter.AbstractHttpMessageConverter;
import org.springframework.http.converter.HttpMessageNotReadableException;

/**
 * Spring {@link org.springframework.http.converter.HttpMessageConverter}
 * for one RDF wire format. Subclasses pin the media type and Jena
 * {@link RDFFormat}; everything else (DTO walking, predicate
 * lookup, model construction) lives in {@link RdfMapper}.
 *
 * <p>Read side currently only sketches: returns {@code null}. The
 * demo controllers all return server-side data (501 → hardcoded), so
 * inbound RDF is not exercised in this MVP. Wiring deserialisation
 * is a follow-up — Jena {@code RDFParser} produces a Model, then
 * subjects matching a known {@code rdf:type} dispatch to the right
 * Java DTO via {@link SpecRegistry#classIri(String)}.
 */
public class RdfHttpMessageConverter
        extends AbstractHttpMessageConverter<Object> {

    public static final MediaType TEXT_TURTLE = new MediaType("text", "turtle");
    public static final MediaType APPLICATION_LD_JSON =
            new MediaType("application", "ld+json");
    public static final MediaType APPLICATION_RDF_XML =
            new MediaType("application", "rdf+xml");

    private final RdfMapper mapper;
    private final RDFFormat format;

    public RdfHttpMessageConverter(
            RdfMapper mapper, RDFFormat format, MediaType mediaType) {
        super(mediaType);
        this.mapper = mapper;
        this.format = format;
    }

    /** Turtle converter — ``text/turtle`` ↔ Jena {@code TURTLE_PRETTY}. */
    public static RdfHttpMessageConverter turtle(RdfMapper mapper) {
        return new RdfHttpMessageConverter(
                mapper, RDFFormat.TURTLE_PRETTY, TEXT_TURTLE);
    }

    /** JSON-LD converter — ``application/ld+json`` ↔ Jena
     *  {@code JSONLD11_PRETTY}. */
    public static RdfHttpMessageConverter jsonLd(RdfMapper mapper) {
        return new RdfHttpMessageConverter(
                mapper, RDFFormat.JSONLD11_PRETTY, APPLICATION_LD_JSON);
    }

    /** RDF/XML converter — ``application/rdf+xml`` ↔ Jena
     *  {@code RDFXML_PRETTY}. */
    public static RdfHttpMessageConverter rdfXml(RdfMapper mapper) {
        return new RdfHttpMessageConverter(
                mapper, RDFFormat.RDFXML_PRETTY, APPLICATION_RDF_XML);
    }

    @Override
    protected boolean supports(Class<?> clazz) {
        // We accept anything — the mapper silently emits no triples
        // for classes outside the spec, and Spring will refuse the
        // converter pairing if the response type is unannotated.
        return true;
    }

    @Override
    public boolean canRead(Class<?> clazz, MediaType mediaType) {
        return super.canRead(clazz, mediaType);
    }

    @Override
    protected Object readInternal(
            Class<?> clazz, HttpInputMessage inputMessage)
            throws IOException, HttpMessageNotReadableException {
        // Parse the request body into a Jena Model in the matching
        // syntax, then dispatch to the spec-driven inverse mapper.
        org.apache.jena.rdf.model.Model model =
                org.apache.jena.rdf.model.ModelFactory
                        .createDefaultModel();
        try {
            org.apache.jena.riot.RDFParser
                    .source(inputMessage.getBody())
                    .lang(format.getLang())
                    .parse(model);
        } catch (Exception e) {
            throw new HttpMessageNotReadableException(
                    "Failed to parse RDF request body as " + format,
                    e,
                    inputMessage);
        }
        Object dto = mapper.fromModel(model, clazz);
        if (dto == null) {
            throw new HttpMessageNotReadableException(
                    "No subject of type "
                            + clazz.getSimpleName()
                            + " found in RDF body",
                    inputMessage);
        }
        return dto;
    }

    @Override
    protected void writeInternal(Object payload, HttpOutputMessage out)
            throws IOException {
        if (payload instanceof Iterable<?> iter) {
            // Spring lists become rdf:Bag-style if you let Jena
            // default; we keep it flat — emit each item's triples
            // in turn into a single Model so a Catalog response
            // carrying List<Catalog> serialises as a graph union.
            Model model = org.apache.jena.rdf.model.ModelFactory
                    .createDefaultModel();
            for (Object item : iter) {
                mapper.addToModel(item, model);
            }
            org.apache.jena.riot.RDFDataMgr.write(
                    out.getBody(), model, format);
            return;
        }
        Model model = mapper.toModel(payload);
        org.apache.jena.riot.RDFDataMgr.write(
                out.getBody(), model, format);
    }

    @Override
    public List<MediaType> getSupportedMediaTypes() {
        return super.getSupportedMediaTypes();
    }
}
