"""CLI for generating OpenAPI specs from LinkML schemas."""

import argparse
import sys

from linkml_runtime.utils.schemaview import SchemaView

from linkml_openapi.generator import OpenAPIGenerator


def main():
    parser = argparse.ArgumentParser(
        description="Generate OpenAPI 3.1 specification from a LinkML schema"
    )
    parser.add_argument("schema", help="Path to LinkML schema YAML file")
    parser.add_argument("-o", "--output", help="Output file (default: stdout)", default=None)
    parser.add_argument(
        "-f",
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format (default: yaml)",
    )
    parser.add_argument("--title", help="API title (default: schema name)")
    parser.add_argument("--version", default="1.0.0", help="API version")
    parser.add_argument("--server-url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument(
        "--classes",
        nargs="*",
        help="Only generate endpoints for these classes (default: auto-detect)",
    )

    args = parser.parse_args()

    sv = SchemaView(args.schema)
    generator = OpenAPIGenerator(
        sv,
        title=args.title,
        version=args.version,
        server_url=args.server_url,
        resource_filter=args.classes,
    )

    output = generator.serialize(format=args.format)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
