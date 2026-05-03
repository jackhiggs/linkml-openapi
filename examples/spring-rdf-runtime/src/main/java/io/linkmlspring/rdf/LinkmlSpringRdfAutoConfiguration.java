package io.linkmlspring.rdf;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.context.annotation.Bean;

/**
 * Wires the {@link SpecRegistry}, {@link RdfMapper}, and three
 * RDF {@link RdfHttpMessageConverter}s into the Spring context.
 *
 * <p>Spring Boot auto-detects this class via the
 * {@code AutoConfiguration.imports} META-INF file. Applications
 * that pull this artefact as a dependency get the converters
 * registered automatically; no XML, no @Import.
 *
 * <p>The OpenAPI spec location defaults to {@code classpath:openapi.yaml}
 * (the file produced by {@code gen-spring-server}'s sidecar emit)
 * but is overridable via {@code linkml.rdf.spec-classpath}.
 */
@AutoConfiguration
public class LinkmlSpringRdfAutoConfiguration {

    @Bean
    public SpecRegistry linkmlRdfSpecRegistry(
            @Value("${linkml.rdf.spec-classpath:/openapi.yaml}")
            String specClasspath) {
        return SpecRegistry.load(specClasspath);
    }

    @Bean
    public RdfMapper linkmlRdfMapper(SpecRegistry registry) {
        return new RdfMapper(registry);
    }

    @Bean
    public RdfHttpMessageConverter turtleHttpMessageConverter(RdfMapper mapper) {
        return RdfHttpMessageConverter.turtle(mapper);
    }

    @Bean
    public RdfHttpMessageConverter jsonLdHttpMessageConverter(RdfMapper mapper) {
        return RdfHttpMessageConverter.jsonLd(mapper);
    }

    @Bean
    public RdfHttpMessageConverter rdfXmlHttpMessageConverter(RdfMapper mapper) {
        return RdfHttpMessageConverter.rdfXml(mapper);
    }
}
