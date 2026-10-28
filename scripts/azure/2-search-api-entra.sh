#!/usr/bin/env bash
# Step 2 (Azure Cloud Shell, Bash; needs an Entra admin who can grant consent):
# register the search API (scope Communications.Search, app roles
# Communications.Reader.All / .Own, assignment required), one client app per
# agent platform, and give the named people the Reader.All role.
#
#   READERS="matthews@finishessolutions.com chuckc@finishesetc.net" \
#     bash scripts/azure/2-search-api-entra.sh
#
# Each run issues a NEW client secret per platform and invalidates the old one
# (rerun to rotate). Secrets are written to ~/fs-ci-mcp-client-secrets.txt
# (readable only by you), never printed. Paste each into its platform's connector
# settings, then delete the file. Never paste secrets into chat, email or GitHub.
set -euo pipefail
: "${READERS:?set READERS to the sign-in addresses allowed to read all enrolled mail}"
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }

API_APP_ID="$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)"
if [ -z "$API_APP_ID" ]; then
  API_APP_ID="$(az ad app create --display-name "$API_NAME" --sign-in-audience AzureADMyOrg \
    --query appId -o tsv)"
  echo "created API app $API_APP_ID"
fi
API_OBJ="$(az ad app show --id "$API_APP_ID" --query id -o tsv)"
API_URI="api://$API_APP_ID"
az ad app update --id "$API_APP_ID" --identifier-uris "$API_URI"

SCOPE_ID="$(az ad app show --id "$API_APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='Communications.Search'].id | [0]" -o tsv)"
if [ -z "$SCOPE_ID" ]; then
  SCOPE_ID="$(uuid)"; ALL_ID="$(uuid)"; OWN_ID="$(uuid)"
  graph PATCH "applications/$API_OBJ" "$(cat <<JSON
{"api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": [{
   "id": "$SCOPE_ID", "value": "Communications.Search", "type": "User", "isEnabled": true,
   "adminConsentDisplayName": "Search Finishes email evidence",
   "adminConsentDescription": "Search email the signed-in person is authorized to read.",
   "userConsentDisplayName": "Search Finishes email evidence",
   "userConsentDescription": "Search email you are authorized to read."}]},
 "appRoles": [
  {"id": "$ALL_ID", "value": "Communications.Reader.All", "allowedMemberTypes": ["User"],
   "displayName": "Communications Reader (all enrolled mailboxes)",
   "description": "Read email from every enrolled mailbox.", "isEnabled": true},
  {"id": "$OWN_ID", "value": "Communications.Reader.Own", "allowedMemberTypes": ["User"],
   "displayName": "Communications Reader (own mailbox)",
   "description": "Read email from your own mailbox and explicit grants.", "isEnabled": true}]}
JSON
)"
  echo "scope and app roles added"
fi
ALL_ID="$(az ad app show --id "$API_APP_ID" \
  --query "appRoles[?value=='Communications.Reader.All'].id | [0]" -o tsv)"

API_SP="$(az ad sp list --filter "appId eq '$API_APP_ID'" --query "[0].id" -o tsv)"
[ -n "$API_SP" ] || API_SP="$(az ad sp create --id "$API_APP_ID" --query id -o tsv)"
az ad sp update --id "$API_SP" --set appRoleAssignmentRequired=true

for upn in $READERS; do
  oid="$(az ad user show --id "$upn" --query id -o tsv)"
  graph POST "servicePrincipals/$API_SP/appRoleAssignedTo" \
    "{\"principalId\":\"$oid\",\"resourceId\":\"$API_SP\",\"appRoleId\":\"$ALL_ID\"}" >/dev/null 2>&1 \
    && echo "Reader.All -> $upn" || echo "Reader.All -> $upn (already assigned)"
done

# One confidential client per platform. Redirect URIs are what each platform
# documented when this was written; check them in the platform's connector screen.
client() {  # client <label> <redirect uri>...
  local label="$1"; shift
  local name="FS Communications MCP - $label" id
  id="$(az ad app list --display-name "$name" --query "[0].appId" -o tsv)"
  if [ -z "$id" ]; then
    id="$(az ad app create --display-name "$name" --sign-in-audience AzureADMyOrg \
      --web-redirect-uris "$@" --query appId -o tsv)"
    az ad sp create --id "$id" --only-show-errors >/dev/null
  else
    az ad app update --id "$id" --web-redirect-uris "$@"
  fi
  az ad app permission add --id "$id" --api "$API_APP_ID" --api-permissions "$SCOPE_ID=Scope" \
    --only-show-errors 2>/dev/null || true
  local client_sp
  client_sp="$(az ad sp list --filter "appId eq '$id'" --query "[0].id" -o tsv)"
  # Tenant-wide consent through Microsoft Graph (the `az ad app permission
  # admin-consent` endpoint does not accept Cloud Shell's sign-in).
  consent "$client_sp" "$API_SP" "Communications.Search"
  consent "$client_sp" "$GRAPH_SP" "openid profile offline_access"
  local secret
  secret="$(az ad app credential reset --id "$id" --display-name mcp --years 1 \
    --query password -o tsv --only-show-errors)"
  printf '[%s]\nClient ID:     %s\nClient secret: %s\n\n' "$label" "$id" "$secret" >>"$SECRETS"
  printf '  %-15s client ID %s (new secret written to %s)\n' "$label" "$id" "$SECRETS"
}
consent() {  # consent <client sp id> <resource sp id> <space-separated scopes>
  local existing
  existing="$(az rest --method GET --only-show-errors --query "value[0].id" -o tsv \
    --uri "https://graph.microsoft.com/v1.0/oauth2PermissionGrants?\$filter=clientId%20eq%20'$1'%20and%20resourceId%20eq%20'$2'%20and%20consentType%20eq%20'AllPrincipals'")"
  if [ -n "$existing" ] && [ "$existing" != "None" ]; then
    graph PATCH "oauth2PermissionGrants/$existing" "{\"scope\":\"$3\"}" >/dev/null
  else
    graph POST oauth2PermissionGrants \
      "{\"clientId\":\"$1\",\"consentType\":\"AllPrincipals\",\"resourceId\":\"$2\",\"scope\":\"$3\"}" >/dev/null
  fi
}
GRAPH_SP="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
SECRETS="$HOME/fs-ci-mcp-client-secrets.txt"
( umask 077; : >"$SECRETS" )
echo
echo "Connector settings (all platforms):"
echo "  MCP server URL:     https://$(app_host "$SRCH_APP")/mcp"
echo "  Authorization URL:  https://login.microsoftonline.com/$TENANT/oauth2/v2.0/authorize"
echo "  Token URL:          https://login.microsoftonline.com/$TENANT/oauth2/v2.0/token"
echo "  Scope:              $API_URI/Communications.Search offline_access"
client "Claude" "https://claude.ai/api/mcp/auth_callback"
client "ChatGPT" "https://chatgpt.com/connector_platform_oauth_redirect"
client "Copilot Studio" "https://global.consent.azure-apim.net/redirect"

az functionapp config appsettings set -g "$RG" -n "$SRCH_APP" --only-show-errors >/dev/null \
  --settings "SEARCH_API_CLIENT_ID=$API_APP_ID" "SEARCH_API_APP_ID_URI=$API_URI"
echo
echo "Search app configured (API $API_APP_ID)."
echo "Secrets: $SECRETS (only you can read it). Paste each into its platform, then: rm $SECRETS"
