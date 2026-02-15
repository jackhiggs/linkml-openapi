#!/usr/bin/env bash
# Regenerate committed OpenAPI outputs from their LinkML schemas.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for schema in "$SCRIPT_DIR"/*/schema.yaml; do
    dir="$(dirname "$schema")"
    name="$(basename "$dir")"
    echo "Generating $name ..."
    gen-openapi "$schema" > "$dir/openapi.yaml"
done

echo "Done."
