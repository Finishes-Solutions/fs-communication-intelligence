#!/usr/bin/env bash
# Which Function Apps a set of changed files needs redeployed (used by
# .github/workflows/deploy-dev.yml). Reads changed paths on stdin, one per
# line; prints ingestion=, search= and admin= true/false lines.
#
# - The admin console's own code (apps/admin, packages/ci_admin) deploys only admin.
# - apps/ingestion or apps/search deploys only that app.
# - Shared packages, dependencies and the build script deploy all three: every
#   app packages and imports them (ingestion and search reach ci_retrieval,
#   ci_email and ci_processing through ci_cli).
# - Anything else (docs, tests, scripts, workflows) deploys nothing.
#   Passing "ALL" as a line deploys everything (first push, manual run).
set -euo pipefail

ingestion=false search=false admin=false
while IFS= read -r path || [ -n "$path" ]; do  # also the last line without a newline
  case "$path" in
    ALL) ingestion=true search=true admin=true ;;
    apps/admin/* | packages/ci_admin/*) admin=true ;;
    apps/ingestion/*) ingestion=true ;;
    apps/search/*) search=true ;;
    packages/* | pyproject.toml | uv.lock | scripts/build-function-app.sh)
      ingestion=true search=true admin=true ;;
  esac
done
echo "ingestion=$ingestion"
echo "search=$search"
echo "admin=$admin"
