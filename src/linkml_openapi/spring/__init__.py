"""Direct LinkML → Spring server emitter.

Skips the OpenAPI middleman for projects targeting Spring services. The
spec→openapi-generator→springdoc round-trip loses polymorphism
(``oneOf`` collapses, discriminator pinning drops) because OpenAPI's
expressiveness is a subset of LinkML's. A direct emitter owns the
full pipeline: LinkML's ``is_a`` → Java ``extends``, LinkML's
discriminator → Jackson ``@JsonTypeInfo`` + ``@JsonSubTypes``, RDF
identity → Javadoc + optional vendor annotations.

Output: a tree of ``.java`` source files (DTOs + Spring controller
interfaces) ready to drop into a Maven/Gradle Spring Boot project.
"""

from linkml_openapi.spring.generator import SpringServerGenerator

__all__ = ["SpringServerGenerator"]
