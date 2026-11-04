#!/usr/bin/env bash
# Step 11 (Azure Cloud Shell, Bash, signed in as svc_automation):
# M1 of docs/MEETINGS_AND_CALLS_PLAN.md. Puts the automation account's Fireflies
# API key into Key Vault (D-M2), lets the ingestion identity read that one secret,
# and points the ingestion app at it with capture OFF. Nothing is captured until
# the steps printed at the end are done.
#
# The key is read from the terminal without echo and goes straight to Key Vault.
# It is never printed, written to disk or committed. Re-run with --rotate to
# replace it.
set -euo pipefail
source "$(dirname "$0")/common.sh"
SECRET_NAME=fireflies-api-key
ROTATE=false; KEY_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --rotate) ROTATE=true ;;
    --key-only) KEY_ONLY=true ;;  # store the key, leave the ingestion app alone (no restart)
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done

echo "== Key Vault $KV"
KV_ID="$(az keyvault show -g "$RG" -n "$KV" --query id -o tsv)"
RBAC="$(az keyvault show -g "$RG" -n "$KV" --query properties.enableRbacAuthorization -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv)"
ING_P="$(mi "$ING_MI" principalId)"
if [ "$RBAC" = "true" ]; then
  role "$ME" "Key Vault Secrets Officer" "$KV_ID" >/dev/null || true
  role "$ING_P" "Key Vault Secrets User" "$KV_ID"
else
  az keyvault set-policy -n "$KV" --object-id "$ME" --secret-permissions get list set --only-show-errors >/dev/null
  az keyvault set-policy -n "$KV" --object-id "$ING_P" --secret-permissions get --only-show-errors >/dev/null
  echo "  access policy ok: $ING_MI may get secrets"
fi

have_secret=false
az keyvault secret show --vault-name "$KV" -n "$SECRET_NAME" --query id -o tsv >/dev/null 2>&1 && have_secret=true
if [ "$have_secret" = false ] || [ "$ROTATE" = true ]; then
  echo "Paste the Fireflies API key of svc_automation@finishessolutions.com (Fireflies -> Settings -> Developer),"
  read -r -s -p "then press Enter (nothing is shown): " FF_KEY; echo
  [ -n "$FF_KEY" ] || { echo "ERROR: empty key" >&2; exit 1; }
  az keyvault secret set --vault-name "$KV" -n "$SECRET_NAME" --value "$FF_KEY" --only-show-errors >/dev/null
  unset FF_KEY
  echo "  secret $SECRET_NAME stored"
else
  echo "  secret $SECRET_NAME already exists (use --rotate to replace it)"
fi
SECRET_URI="$(az keyvault secret show --vault-name "$KV" -n "$SECRET_NAME" --query id -o tsv)"

if [ "$KEY_ONLY" = true ]; then
  cat <<TXT

Key stored; the ingestion app was not changed (changing app settings restarts it, which
stops a running backfill window). To run Fireflies commands from this shell:

  export FIREFLIES_BACKEND=api
  export FIREFLIES_API_KEY="\$(az keyvault secret show --vault-name $KV -n $SECRET_NAME --query value -o tsv)"

Run this script again without --key-only when the app may restart.
TXT
  exit 0
fi

echo "== app settings on $ING_APP (capture off; this restarts the app)"
az functionapp config appsettings set -g "$RG" -n "$ING_APP" --only-show-errors >/dev/null --settings \
  "FIREFLIES_BACKEND=api" "FIREFLIES_API_KEY=@Microsoft.KeyVault(SecretUri=$SECRET_URI)" \
  "FIREFLIES_CAPTURE_ENABLED=false" "FIREFLIES_CAPTURE_DAYS=14" "FIREFLIES_BACKFILL_SINCE=2025-01-01T00:00:00Z"
echo "  FIREFLIES_BACKEND=api, FIREFLIES_API_KEY -> Key Vault reference, FIREFLIES_CAPTURE_ENABLED=false"

cat <<TXT

Done. Next, from a machine with the Azure dev database settings (make azure-env; docs/DEPLOY_AZURE.md 14):

  ci fireflies check                      # the key's user, team size, one listing page (counts only)
  ci fireflies init                       # connection + corpus 'fireflies-meetings' (Reader.All may read)
  ci mode sources email,fireflies         # capture may read Fireflies under the current mode
  ci fireflies inventory                  # the team as capture scopes
  ci fireflies scope include --all-enrolled
  ci fireflies capture --days 14          # a first real window, from this machine
  ci backfill plan --source fireflies --since 2025-01-01 --days 30   # the full history (D-M1)

Then turn the app's rolling capture on: FIREFLIES_CAPTURE_ENABLED=true on $ING_APP.
If 'ci fireflies check' warns that the key is not an admin key, make the automation seat a
Fireflies admin first: a member key lists only its own meetings.
TXT
