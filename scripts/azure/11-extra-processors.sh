#!/usr/bin/env bash
# Extra processing capacity for a backfill without any VM quota (Matthew,
# 2026-11-03): N more Flex Consumption Function Apps running the same ingestion
# code with the same managed identity, storage account and settings as the
# ingestion app, but with capture off. Each one's 5-minute processing timer
# works the same queue (rows are claimed with FOR UPDATE SKIP LOCKED), so they
# add processors, not duplicate work. Nothing new is granted: the identity,
# Exchange scope, database role and AI access are the ingestion app's own.
#
#   bash scripts/azure/11-extra-processors.sh            # create N (3) apps, deploy, start
#   bash scripts/azure/11-extra-processors.sh --status   # the apps and their state
#   bash scripts/azure/11-extra-processors.sh --stop     # delete the extra apps
#
# Settings (export before running): N (3 apps), WORKERS (96 per app).
# Run from the repo root on a checkout of the code the ingestion app runs
# (the package is built locally with Linux wheels, like the deploy workflow).
# Later GitHub deploys update only the main ingestion app: run --stop when the
# backlog is gone, or run this again to redeploy the extras.
set -euo pipefail
source "$(dirname "$0")/common.sh"

N="${N:-3}"
WORKERS="${WORKERS:-96}"
PREFIX="$ING_APP-p"

extras() { az functionapp list -g "$RG" --query "[?starts_with(name, '$PREFIX')].name" -o tsv; }

case "${1:-}" in
  --status)
    az functionapp list -g "$RG" --query "[?starts_with(name, '$PREFIX')].{app:name, state:state}" -o table
    exit 0 ;;
  --stop)
    for app in $(extras); do
      az functionapp delete -g "$RG" -n "$app" --only-show-errors && echo "deleted $app"
    done
    exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--status|--stop]" >&2; exit 2 ;;
esac

mi_id="$(mi "$ING_MI" id)"
root="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== building the ingestion package (Linux wheels, a minute or two)"
bash "$root/scripts/build-function-app.sh" ingestion "$work" >/dev/null
echo "  built $work/ingestion.zip"

echo "== settings from $ING_APP (capture off on the extras)"
az functionapp config appsettings list -g "$RG" -n "$ING_APP" -o json > "$work/all.json"
python3 - "$work/all.json" "$work/settings.json" "$WORKERS" <<'PY'
import json, sys
src, dst, workers = sys.argv[1], sys.argv[2], sys.argv[3]
skip_prefixes = ("WEBSITE_", "SCM_", "FUNCTIONS_", "DEPLOYMENT_STORAGE")
settings = [
    {"name": s["name"], "value": s["value"], "slotSetting": False}
    for s in json.load(open(src))
    if s.get("value") is not None
    and s["name"] != "AzureWebJobsStorage"
    and not s["name"].startswith(skip_prefixes)
]
override = {"CAPTURE_ENABLED": "false", "PROCESSING_ENABLED": "true",
            "PROCESSING_WORKERS": workers}
settings = [s for s in settings if s["name"] not in override]
settings += [{"name": k, "value": v, "slotSetting": False} for k, v in override.items()]
json.dump(settings, open(dst, "w"))
print(f"  {len(settings)} settings")
PY

for i in $(seq 1 "$N"); do
  app="$PREFIX$i"
  echo "== $app"
  if az functionapp show -g "$RG" -n "$app" >/dev/null 2>&1; then
    echo "  app exists"
  else
    az functionapp create -g "$RG" -n "$app" --flexconsumption-location "$LOC" \
      --runtime python --runtime-version "$PY" --storage-account "$ING_ST" \
      --deployment-storage-auth-type UserAssignedIdentity --deployment-storage-auth-value "$mi_id" \
      --app-insights "$APPI" --instance-memory 4096 --only-show-errors >/dev/null
    echo "  app created"
  fi
  az functionapp identity assign -g "$RG" -n "$app" --identities "$mi_id" --only-show-errors >/dev/null
  az functionapp config appsettings set -g "$RG" -n "$app" --settings "@$work/settings.json" \
    --only-show-errors -o none
  az functionapp config appsettings delete -g "$RG" -n "$app" --setting-names AzureWebJobsStorage \
    --only-show-errors -o none 2>/dev/null || true
  az functionapp deployment source config-zip -g "$RG" -n "$app" --src "$work/ingestion.zip" \
    --only-show-errors -o none
  echo "  deployed; processing starts at its next 5-minute tick"
done

cat <<OUT

$N extra processor(s) running ($WORKERS workers each). Watch with:  ci runs --limit 30
(each app records its own 'process' runs). Remove them when the backlog is gone:
  bash scripts/azure/11-extra-processors.sh --stop
OUT
