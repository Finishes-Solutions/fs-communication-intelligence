#!/usr/bin/env bash
# Step 4 (Azure Cloud Shell, Bash; needs an Entra admin who can grant consent):
# the admin console. Creates its own managed identity, storage account and Flex
# Consumption Function App; adds the scope and app role Communications.Admin to
# the existing search API registration (keeping everything already there);
# gives that role to the named admins; registers a browser (SPA) sign-in app
# with no secret; and lets GitHub deploy the app. Safe to re-run.
#
#   CHAT_DEPLOYMENT=gpt-4.1-mini ADMINS="matthews@finishessolutions.com" \
#     bash scripts/azure/4-admin-console.sh
#
# Afterwards: `make azure-db-principals` (database login for the new identity)
# and `make azure-migrate` (schedule and run-history tables), then add the two
# GitHub secrets this prints and rerun the deploy workflow.
set -euo pipefail
: "${CHAT_DEPLOYMENT:?set CHAT_DEPLOYMENT to the name of your Azure OpenAI chat deployment}"
: "${ADMINS:?set ADMINS to the sign-in addresses allowed into the admin console}"
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }

echo "== identity, storage and Function App"
if ! az identity show -g "$RG" -n "$ADM_MI" >/dev/null 2>&1; then
  az identity create -g "$RG" -n "$ADM_MI" -l "$LOC" --only-show-errors >/dev/null
  echo "  identity created"
fi
if ! az storage account show -g "$RG" -n "$ADM_ST" >/dev/null 2>&1; then
  az storage account create -g "$RG" -n "$ADM_ST" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false --only-show-errors >/dev/null
  echo "  storage created"
fi
ADM_ID="$(mi "$ADM_MI" id)"; ADM_P="$(mi "$ADM_MI" principalId)"; ADM_CLIENT="$(mi "$ADM_MI" clientId)"
ST_ID="$(az storage account show -g "$RG" -n "$ADM_ST" --query id -o tsv)"
for r in "Storage Blob Data Owner" "Storage Queue Data Contributor" "Storage Table Data Contributor"; do
  role "$ADM_P" "$r" "$ST_ID"
done
if az functionapp show -g "$RG" -n "$ADM_APP" >/dev/null 2>&1; then
  echo "  app exists"
else
  az functionapp create -g "$RG" -n "$ADM_APP" --flexconsumption-location "$LOC" \
    --runtime python --runtime-version "$PY" --storage-account "$ADM_ST" \
    --deployment-storage-auth-type UserAssignedIdentity --deployment-storage-auth-value "$ADM_ID" \
    --app-insights "$APPI" --only-show-errors >/dev/null
  echo "  app created"
fi
az functionapp identity assign -g "$RG" -n "$ADM_APP" --identities "$ADM_ID" --only-show-errors >/dev/null
ADM_HOST="$(app_host "$ADM_APP")"

echo "== Communications.Admin scope and app role on \"$API_NAME\""
API_APP_ID="$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)"
[ -n "$API_APP_ID" ] || { echo "run scripts/azure/2-search-api-entra.sh first" >&2; exit 1; }
API_OBJ="$(az ad app show --id "$API_APP_ID" --query id -o tsv)"
API_URI="api://$API_APP_ID"
# Append to the existing scopes and roles. Graph replaces a complex property
# whole on PATCH, so send back the entire "api" object (it also holds
# requestedAccessTokenVersion = 2, which the token checks depend on).
BODY="$(graph GET "applications/$API_OBJ?\$select=api,appRoles" | python3 -c '
import json, sys, uuid
app = json.load(sys.stdin)
scopes = app["api"]["oauth2PermissionScopes"]
roles = app["appRoles"]
changed = False
if not any(s["value"] == "Communications.Admin" for s in scopes):
    scopes.append({"id": str(uuid.uuid4()), "value": "Communications.Admin", "type": "Admin",
        "isEnabled": True, "adminConsentDisplayName": "Administer email intelligence",
        "adminConsentDescription": "Use the admin console: schedule, scope, run history, lab."})
    changed = True
if not any(r["value"] == "Communications.Admin" for r in roles):
    roles.append({"id": str(uuid.uuid4()), "value": "Communications.Admin",
        "allowedMemberTypes": ["User"], "displayName": "Communications Admin (console)",
        "description": "Use the email intelligence admin console.", "isEnabled": True})
    changed = True
print(json.dumps({"api": app["api"], "appRoles": roles}) if changed else "")
')"
if [ -n "$BODY" ]; then graph PATCH "applications/$API_OBJ" "$BODY" >/dev/null; echo "  added"; else echo "  present"; fi
ADMIN_SCOPE_ID="$(az ad app show --id "$API_APP_ID" \
  --query "api.oauth2PermissionScopes[?value=='Communications.Admin'].id | [0]" -o tsv)"
ADMIN_ROLE_ID="$(az ad app show --id "$API_APP_ID" \
  --query "appRoles[?value=='Communications.Admin'].id | [0]" -o tsv)"
API_SP="$(az ad sp list --filter "appId eq '$API_APP_ID'" --query "[0].id" -o tsv)"
for upn in $ADMINS; do
  oid="$(az ad user show --id "$upn" --query id -o tsv)"
  graph POST "servicePrincipals/$API_SP/appRoleAssignedTo" \
    "{\"principalId\":\"$oid\",\"resourceId\":\"$API_SP\",\"appRoleId\":\"$ADMIN_ROLE_ID\"}" >/dev/null 2>&1 \
    && echo "  Admin -> $upn" || echo "  Admin -> $upn (already assigned)"
done

echo "== browser sign-in app (SPA, no secret)"
SPA_NAME="FS Communications Admin Console"
SPA_ID="$(az ad app list --display-name "$SPA_NAME" --query "[0].appId" -o tsv)"
if [ -z "$SPA_ID" ]; then
  SPA_ID="$(az ad app create --display-name "$SPA_NAME" --sign-in-audience AzureADMyOrg \
    --query appId -o tsv)"
  az ad sp create --id "$SPA_ID" --only-show-errors >/dev/null
  echo "  created $SPA_ID"
fi
SPA_OBJ="$(az ad app show --id "$SPA_ID" --query id -o tsv)"
graph PATCH "applications/$SPA_OBJ" "{\"spa\": {\"redirectUris\": [\"https://$ADM_HOST/\"]}}" >/dev/null
az ad app permission add --id "$SPA_ID" --api "$API_APP_ID" \
  --api-permissions "$ADMIN_SCOPE_ID=Scope" --only-show-errors 2>/dev/null || true
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
SPA_SP="$(az ad sp list --filter "appId eq '$SPA_ID'" --query "[0].id" -o tsv)"
GRAPH_SP="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
# Consent lets the app ask for the scope; only people with the Admin app role get past the API.
consent "$SPA_SP" "$API_SP" "Communications.Admin"
consent "$SPA_SP" "$GRAPH_SP" "openid profile"
echo "  redirect https://$ADM_HOST/ and consent ok"

echo "== app settings"
PGHOST="$PG.postgres.database.azure.com"
az functionapp config appsettings set -g "$RG" -n "$ADM_APP" --only-show-errors >/dev/null --settings \
  "AzureWebJobsStorage__accountName=$ADM_ST" "AzureWebJobsStorage__credential=managedidentity" \
  "AzureWebJobsStorage__clientId=$ADM_CLIENT" "AZURE_CLIENT_ID=$ADM_CLIENT" \
  "AzureWebJobsDisableHomepage=true" \
  "DEPLOYMENT_ENVIRONMENT=development" "DATABASE_AUTH=entra" "PGHOST=$PGHOST" "PGPORT=5432" \
  "PGUSER=$ADM_MI" "PGDATABASE=ci" "PGSSLMODE=require" \
  "AZURE_OPENAI_ENDPOINT=$(endpoint "$AOAI")" "AZURE_OPENAI_CHAT_DEPLOYMENT=$CHAT_DEPLOYMENT" \
  "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large" \
  "AZURE_LANGUAGE_ENDPOINT=$(endpoint "$LANG_SVC")" \
  "AZURE_CONTENT_SAFETY_ENDPOINT=$(endpoint "$SAFE")" \
  "ENTRA_TENANT_ID=$TENANT" "SEARCH_API_CLIENT_ID=$API_APP_ID" "SEARCH_API_APP_ID_URI=$API_URI" \
  "ADMIN_SPA_CLIENT_ID=$SPA_ID"
az functionapp config appsettings delete -g "$RG" -n "$ADM_APP" --setting-names AzureWebJobsStorage \
  --only-show-errors >/dev/null || true
echo "  settings ok"

echo "== access: AI services for search and the lab, GitHub deploy"
cog() { az cognitiveservices account show -g "$RG" -n "$1" --query id -o tsv; }
role "$ADM_P" "Cognitive Services OpenAI User" "$(cog "$AOAI")"
role "$ADM_P" "Cognitive Services User" "$(cog "$LANG_SVC")"
role "$ADM_P" "Cognitive Services User" "$(cog "$SAFE")"
role "$(mi "$GH_MI" principalId)" "Website Contributor" \
  "$(az functionapp show -g "$RG" -n "$ADM_APP" --query id -o tsv)"

cat <<OUT

Done. Next:
  1. make azure-db-principals    (database login for $ADM_MI: ci_operator + ci_search)
  2. make azure-migrate          (adds the schedule and run-history tables)
  3. Add two SECRETS to the GitHub environment "development":
       ADMIN_FUNCTION_APP   $ADM_APP
       ADMIN_PUBLIC_URL     https://$ADM_HOST
  4. Rerun "Deploy (development)" in GitHub Actions, then open https://$ADM_HOST/
OUT
