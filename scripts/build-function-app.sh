#!/usr/bin/env bash
# Package one Function App (ingestion | search | admin) as a zip ready for Flex Consumption.
# A deployed Function App cannot see sibling folders, so the shared packages and
# all dependencies (Linux x86_64 wheels for the target Python) go inside the zip.
#
#   bash scripts/build-function-app.sh ingestion dist      # -> dist/ingestion.zip
#   PYTHON_VERSION=3.14 bash scripts/build-function-app.sh search dist
set -euo pipefail

app="${1:?usage: build-function-app.sh ingestion|search|admin [outdir]}"
out="${2:-dist}"
python_version="${PYTHON_VERSION:-3.14}"
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$app" in ingestion|search|admin) ;; *) echo "unknown app: $app" >&2; exit 2 ;; esac

build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -R "$root/apps/$app/." "$build/"
for pkg in ci_core ci_email ci_fireflies ci_processing ci_retrieval ci_cli ci_admin; do
  cp -R "$root/packages/$pkg" "$build/$pkg"
done

(cd "$root" && uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt) \
  > "$build/requirements.txt"
echo "azure-functions==2.3.0" >> "$build/requirements.txt"
uv pip install --quiet --no-config -r "$build/requirements.txt" \
  --target "$build/.python_packages/lib/site-packages" \
  --python-version "$python_version" --python-platform x86_64-manylinux_2_28

find "$build" -name __pycache__ -type d -prune -exec rm -rf {} +
mkdir -p "$out"
out="$(cd "$out" && pwd)"
rm -f "$out/$app.zip"
(cd "$build" && zip -qr "$out/$app.zip" .)
echo "built $out/$app.zip ($(du -h "$out/$app.zip" | cut -f1))"
