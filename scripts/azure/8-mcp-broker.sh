#!/usr/bin/env bash
# Step 8 (Azure Cloud Shell, Bash; an Entra admin who can grant consent and an
# Owner of the resource group): turn on the MCP sign-in broker, so Claude, ChatGPT
# and Copilot Studio connect with only the MCP URL and a Microsoft 365 sign-in.
# Safe to re-run; add --rotate to replace the broker's client secret (everyone
# connected through the broker then signs in again).
#
#   bash scripts/azure/8-mcp-broker.sh
#
# What it does:
#  1. Makes sure the search API still refuses everyone not assigned to it
#     ("assignment required"; assign people with scripts/azure/people.sh).
#  2. Registers one confidential client, "FS Communications MCP - Broker", whose
#     only return address is https://<search app>/oauth/callback and whose only
#     permission is Communications.Search (granted for the organization).
#  3. Creates its client secret straight into Key Vault ($KV, secret
#     mcp-broker-client-secret). The secret is never printed or written to disk
#     outside a private temporary file that is deleted at once.
#  4. Lets the search app's identity read that one secret, and points the search
#     app at it with a Key Vault reference (MCP_BROKER_CLIENT_SECRET).
#  5. Tells the admin console the broker is on (Connections page).
# Turn the broker off again: delete the app settings MCP_BROKER_CLIENT_ID and
# MCP_BROKER_CLIENT_SECRET on the search app; manual connections keep working.
set -euo pipefail
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }
ROTATE=false; [ "${1:-}" = "--rotate" ] && ROTATE=true
SECRET_NAME=mcp-broker-client-secret
BROKER_NAME="FS Communications MCP - Broker"

echo "== search API"
API_APP_ID="$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)"
[ -n "$API_APP_ID" ] || { echo "ERROR: \"$API_NAME\" not found; run scripts/azure/2-search-api-entra.sh first" >&2; exit 1; }
API_SP="$(az ad sp list --filter "appId eq '$API_APP_ID'" --query "[0].id" -o tsv)"
SCOPE_ID="$(az ad app show --id "$API_APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='Communications.Search'].id | [0]" -o tsv)"
az ad sp update --id "$API_SP" --set appRoleAssignmentRequired=true
echo "  assignment required: $(az ad sp show --id "$API_SP" --query appRoleAssignmentRequired -o tsv)"
SRCH_HOST="$(app_host "$SRCH_APP")"
CALLBACK="https://$SRCH_HOST/oauth/callback"

echo "== broker app registration"
BROKER_ID="$(az ad app list --display-name "$BROKER_NAME" --query "[0].appId" -o tsv)"
if [ -z "$BROKER_ID" ]; then
  BROKER_ID="$(az ad app create --display-name "$BROKER_NAME" --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "$CALLBACK" --query appId -o tsv)"
  az ad sp create --id "$BROKER_ID" --only-show-errors >/dev/null
  echo "  created $BROKER_ID"
else
  az ad app update --id "$BROKER_ID" --web-redirect-uris "$CALLBACK"  # exactly one address
  echo "  exists $BROKER_ID"
fi
az ad app permission add --id "$BROKER_ID" --api "$API_APP_ID" --api-permissions "$SCOPE_ID=Scope" \
  --only-show-errors 2>/dev/null || true
BROKER_SP="$(az ad sp list --filter "appId eq '$BROKER_ID'" --query "[0].id" -o tsv)"
# Organization-wide consent for the search scope only, through Microsoft Graph.
existing="$(az rest --method GET --only-show-errors --query "value[0].id" -o tsv \
  --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId%20eq%20'$BROKER_SP'%20and%20resourceId%20eq%20'$API_SP'%20and%20consentType%20eq%20'AllPrincipals'")"
if [ -n "$existing" ] && [ "$existing" != "None" ]; then
  graph PATCH "oauth2PermissionGrants/$existing" '{"scope":"Communications.Search"}' >/dev/null
else
  graph POST oauth2PermissionGrants \
    "{\"clientId\":\"$BROKER_SP\",\"consentType\":\"AllPrincipals\",\"resourceId\":\"$API_SP\",\"scope\":\"Communications.Search\"}" >/dev/null
fi
echo "  consent ok: Communications.Search"

echo "== client secret in Key Vault ($KV)"
KV_ID="$(az keyvault show -g "$RG" -n "$KV" --query id -o tsv)"
RBAC="$(az keyvault show -g "$RG" -n "$KV" --query properties.enableRbacAuthorization -o tsv)"
ME="$(az ad signed-in-user show --query id -o tsv)"
SRCH_MI_P="$(mi "$SRCH_MI" principalId)"
if [ "$RBAC" = "true" ]; then
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$KV_ID" --only-show-errors >/dev/null
else
  az keyvault set-policy -n "$KV" --object-id "$ME" --secret-permissions get list set \
    --only-show-errors >/dev/null
fi
have_secret=false
az keyvault secret show --vault-name "$KV" -n "$SECRET_NAME" --query id -o tsv >/dev/null 2>&1 && have_secret=true
if [ "$have_secret" = false ] || [ "$ROTATE" = true ]; then
  tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT; chmod 600 "$tmp"
  # Without --append, older broker secrets stop working (a rotation).
  az ad app credential reset --id "$BROKER_ID" --display-name broker --years 1 \
    --query password -o tsv --only-show-errors | tr -d '\n' >"$tmp"
  for attempt in 1 2 3 4 5 6; do  # a new Key Vault role can take a minute to apply
    if az keyvault secret set --vault-name "$KV" -n "$SECRET_NAME" --file "$tmp" \
      --only-show-errors -o none 2>/dev/null; then break; fi
    [ "$attempt" = 6 ] && { echo "ERROR: could not write to $KV; check your Key Vault access" >&2; exit 1; }
    sleep 20
  done
  rm -f "$tmp"
  echo "  new secret stored (never printed)"
else
  echo "  secret already stored (use --rotate to replace it)"
fi
SECRET_ID="$(az keyvault secret show --vault-name "$KV" -n "$SECRET_NAME" --query id -o tsv)"
if [ "$RBAC" = "true" ]; then
  # Read access to this one secret only, not the whole vault.
  role "$SRCH_MI_P" "Key Vault Secrets User" "$KV_ID/secrets/$SECRET_NAME"
else
  az keyvault set-policy -n "$KV" --object-id "$SRCH_MI_P" --secret-permissions get \
    --only-show-errors >/dev/null
  echo "  access policy ok: $SRCH_MI get"
fi

echo "== search app settings"
SITE_ID="$(az functionapp show -g "$RG" -n "$SRCH_APP" --query id -o tsv)"
az resource update --ids "$SITE_ID" --set "properties.keyVaultReferenceIdentity=$(mi "$SRCH_MI" id)" \
  --only-show-errors -o none
az functionapp config appsettings set -g "$RG" -n "$SRCH_APP" --only-show-errors >/dev/null --settings \
  "MCP_BROKER_CLIENT_ID=$BROKER_ID" \
  "MCP_BROKER_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=${SECRET_ID%/*})"
az functionapp config appsettings set -g "$RG" -n "$ADM_APP" --only-show-errors >/dev/null \
  --settings "MCP_BROKER_CLIENT_ID=$BROKER_ID" 2>/dev/null || true
echo "  $SRCH_APP uses the broker; it restarts now"

echo "== check"
for attempt in $(seq 1 12); do
  if curl -fsS "https://$SRCH_HOST/.well-known/oauth-authorization-server" 2>/dev/null \
    | grep -q '"registration_endpoint"'; then
    echo "  OK: https://$SRCH_HOST/.well-known/oauth-authorization-server offers registration"
    echo "Done. Connect with only this URL: https://$SRCH_HOST/mcp"
    exit 0
  fi
  sleep 10
done
echo "  Not on yet. If the search app shows 'Key Vault reference' errors (Function App ->"
echo "  Settings -> Environment variables), wait a few minutes for the role to apply and rerun."
exit 1
