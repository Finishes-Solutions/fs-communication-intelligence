#!/usr/bin/env bash
# Fireflies meetings in the ingestion Function App (Azure Cloud Shell, Bash, repo folder):
#
#   bash scripts/azure/6-fireflies.sh
#
# 1. asks for the Fireflies API key (typed, not echoed, never written to disk or
#    shell history) and stores it in Key Vault as secret "fireflies-api-key";
# 2. lets the ingestion identity read that vault's secrets;
# 3. points the ingestion app at the secret with a Key Vault reference, so the
#    key is never an app-setting value; sets FIREFLIES_ENABLED=false.
# Then apply migrations (make azure-migrate) and run `ci fireflies init`; see
# docs/FIREFLIES_PIPELINE.md section 7. Safe to re-run; press Enter at the
# prompt to keep the stored key.
set -euo pipefail
source "$(dirname "$0")/common.sh"
SECRET="${FIREFLIES_SECRET_NAME:-fireflies-api-key}"

kv_id="$(az keyvault show -n "$KV" -g "$RG" --query id -o tsv)"
rbac="$(az keyvault show -n "$KV" -g "$RG" --query properties.enableRbacAuthorization -o tsv)"
ING_P="$(mi "$ING_MI" principalId)"; ING_ID="$(mi "$ING_MI" id)"

echo "== Key Vault access ($KV, RBAC: $rbac)"
if [ "$rbac" = "true" ]; then
  me="$(az ad signed-in-user show --query id -o tsv)"
  az role assignment create --assignee-object-id "$me" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$kv_id" --only-show-errors >/dev/null
  echo "  role ok: Key Vault Secrets Officer (you, to store the key)"
  role "$ING_P" "Key Vault Secrets User" "$kv_id"
else
  az keyvault set-policy -n "$KV" -g "$RG" --object-id "$ING_P" --secret-permissions get \
    --only-show-errors >/dev/null
  echo "  access policy ok: $ING_MI may get secrets"
fi

echo "== Fireflies API key"
read -rsp "Paste the Fireflies API key (Fireflies -> Settings -> Developer settings), then Enter: " key
echo
if [ -n "$key" ]; then
  # A new role assignment can take a minute to apply; retry briefly.
  for i in 1 2 3 4 5 6; do
    if printf '%s' "$key" | az keyvault secret set --vault-name "$KV" -n "$SECRET" \
         --file /dev/stdin --only-show-errors >/dev/null 2>&1; then
      echo "  stored as secret $SECRET"; break
    fi
    [ "$i" = 6 ] && { echo "ERROR: could not store the secret in $KV" >&2; exit 1; }
    sleep 20
  done
else
  az keyvault secret show --vault-name "$KV" -n "$SECRET" --query id -o tsv >/dev/null \
    || { echo "ERROR: no key given and none stored yet" >&2; exit 1; }
  echo "  kept the stored key"
fi
unset key

echo "== ingestion app settings"
az functionapp update -g "$RG" -n "$ING_APP" --set "keyVaultReferenceIdentity=$ING_ID" \
  --only-show-errors >/dev/null
az functionapp config appsettings set -g "$RG" -n "$ING_APP" --only-show-errors >/dev/null --settings \
  "FIREFLIES_API_KEY=@Microsoft.KeyVault(VaultName=$KV;SecretName=$SECRET)" \
  "FIREFLIES_BACKEND=api" "FIREFLIES_ENABLED=false" "FIREFLIES_LIST_PER_USER=true" \
  "FIREFLIES_RATE_LIMIT_PER_MINUTE=50"
echo "  settings ok (FIREFLIES_ENABLED=false until you turn it on)"

cat <<OUT

Check: Function App $ING_APP -> Settings -> Environment variables -> FIREFLIES_API_KEY
shows a green "Key Vault Reference" tick (it can take a few minutes after a restart).
Next: docs/FIREFLIES_PIPELINE.md section 7 (migrate, ci fireflies init, first capture).
OUT
