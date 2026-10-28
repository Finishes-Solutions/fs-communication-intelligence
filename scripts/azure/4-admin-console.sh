#!/usr/bin/env bash
# Step 4 (Azure Cloud Shell, Bash; needs an Entra admin who can grant consent):
# the admin console and original attachment files, in one go. Safe to re-run:
# it checks what already exists and only creates what is missing.
#
#   CHAT_DEPLOYMENT=gpt-4.1-mini ADMINS="matthews@finishessolutions.com" \
#     bash scripts/azure/4-admin-console.sh
#
# CHECK_ONLY=1 prints what exists and what would be created, and changes nothing.
# Different names? Export them first (see scripts/azure/common.sh), e.g.
# ADM_APP=my-admin-app EVI_ST=mystorage bash scripts/azure/4-admin-console.sh
#
# What it does:
#  1. Admin console: managed identity id-fs-ci-admin-dev, storage account and
#     Flex Consumption Function App; scope + app role Communications.Admin on the
#     existing search API registration (keeping what is there); the role for
#     ADMINS; a browser (SPA) sign-in app with no secret; GitHub deploy access.
#  2. Original files: private container "attachments" in the evidence storage
#     account; the ingestion identity may write, the console identity may read;
#     ATTACHMENT_STORE_ORIGINALS=true on the ingestion app. No malware scanning
#     (Matthew, 2026-10-28; CLAUDE.md section 3a).
#  3. Database (when psql and local/azure-dev.env are present): migrations
#     0005-0006 and the console identity's database login.
# Then add the two GitHub secrets it prints and merge/redeploy.
set -euo pipefail
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }

have() { "$@" >/dev/null 2>&1 && echo "exists " || echo "MISSING"; }
echo "== what is already there"
printf '  %s  resource group     %s\n' "$(have az group show -n "$RG")" "$RG"
printf '  %s  ingestion app      %s\n' "$(have az functionapp show -g "$RG" -n "$ING_APP")" "$ING_APP"
printf '  %s  ingestion identity %s\n' "$(have az identity show -g "$RG" -n "$ING_MI")" "$ING_MI"
printf '  %s  search API (Entra) %s\n' "$( [ -n "$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)" ] && echo "exists " || echo "MISSING")" "$API_NAME"
printf '  %s  admin identity     %s\n' "$(have az identity show -g "$RG" -n "$ADM_MI")" "$ADM_MI"
printf '  %s  admin storage      %s\n' "$(have az storage account show -g "$RG" -n "$ADM_ST")" "$ADM_ST"
printf '  %s  admin app          %s\n' "$(have az functionapp show -g "$RG" -n "$ADM_APP")" "$ADM_APP"
printf '  %s  sign-in app        FS Communications Admin Console\n' "$( [ -n "$(az ad app list --display-name "FS Communications Admin Console" --query "[0].appId" -o tsv)" ] && echo "exists " || echo "MISSING")"
printf '  %s  evidence storage   %s\n' "$(have az storage account show -g "$RG" -n "$EVI_ST")" "$EVI_ST"
printf '  %s  files container    %s/%s\n' "$( [ "$(az storage container-rm exists -g "$RG" --storage-account "$EVI_ST" -n "$EVI_CONTAINER" --query exists -o tsv 2>/dev/null)" = "true" ] && echo "exists " || echo "MISSING")" "$EVI_ST" "$EVI_CONTAINER"
for needed in "$ING_APP:functionapp" "$ING_MI:identity"; do
  name="${needed%%:*}"; kind="${needed#*:}"
  az "$kind" show -g "$RG" -n "$name" >/dev/null 2>&1 || { echo "ERROR: $name is missing; run scripts/azure/1-function-apps.sh first" >&2; exit 1; }
done
if [ "${CHECK_ONLY:-0}" = "1" ]; then echo "CHECK_ONLY=1: nothing changed."; exit 0; fi
: "${CHAT_DEPLOYMENT:?set CHAT_DEPLOYMENT to the name of your Azure OpenAI chat deployment}"
: "${ADMINS:?set ADMINS to the sign-in addresses allowed into the admin console}"

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

echo "== original attachment files: $EVI_ST/$EVI_CONTAINER (private)"
if ! az storage account show -g "$RG" -n "$EVI_ST" >/dev/null 2>&1; then
  az storage account create -g "$RG" -n "$EVI_ST" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false --only-show-errors >/dev/null
  echo "  storage account created"
else
  az storage account update -g "$RG" -n "$EVI_ST" --allow-blob-public-access false \
    --min-tls-version TLS1_2 --only-show-errors >/dev/null
  echo "  storage account exists (public blob access kept off)"
fi
if [ "$(az storage container-rm exists -g "$RG" --storage-account "$EVI_ST" -n "$EVI_CONTAINER" --query exists -o tsv)" != "true" ]; then
  az storage container-rm create -g "$RG" --storage-account "$EVI_ST" -n "$EVI_CONTAINER" \
    --public-access off --only-show-errors >/dev/null
  echo "  container created"
fi
EVI_ID="$(az storage account show -g "$RG" -n "$EVI_ST" --query id -o tsv)"
CONTAINER_SCOPE="$EVI_ID/blobServices/default/containers/$EVI_CONTAINER"
role "$(mi "$ING_MI" principalId)" "Storage Blob Data Contributor" "$CONTAINER_SCOPE"   # writes files
role "$ADM_P" "Storage Blob Data Reader" "$CONTAINER_SCOPE"                            # serves them
if [ "$(az storage account show -g "$RG" -n "$EVI_ST" --query networkRuleSet.defaultAction -o tsv)" = "Deny" ]; then
  echo "  WARNING: $EVI_ST only accepts selected networks. The Function Apps have no VNet, so"
  echo "  storing and serving files will fail until you allow access (Networking -> Public"
  echo "  network access -> Enabled from all networks; Entra sign-in is still required)."
fi
az functionapp config appsettings set -g "$RG" -n "$ING_APP" --only-show-errors >/dev/null --settings \
  "ATTACHMENT_STORE_ORIGINALS=true" "EVIDENCE_BLOB_ACCOUNT=$EVI_ST" "EVIDENCE_BLOB_CONTAINER=$EVI_CONTAINER"
echo "  ingestion app keeps originals from its next deploy"

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
  "ADMIN_SPA_CLIENT_ID=$SPA_ID" "EVIDENCE_BLOB_ACCOUNT=$EVI_ST" "EVIDENCE_BLOB_CONTAINER=$EVI_CONTAINER"
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

echo "== database"
cd "$(dirname "$0")/../.."
if command -v psql >/dev/null && [ -f local/azure-dev.env ]; then
  make azure-migrate AZ_PSQL=psql && make azure-db-principals AZ_PSQL=psql
  DB_DONE=1
else
  echo "  skipped (needs psql and local/azure-dev.env); run from a machine that has them:"
  echo "    make azure-migrate AZ_PSQL=psql && make azure-db-principals AZ_PSQL=psql"
  DB_DONE=0
fi

cat <<OUT

Done.$( [ "$DB_DONE" = 1 ] || echo " Database step still to do (see above).")
Add two SECRETS to the GitHub environment "development"
(GitHub -> repo Settings -> Environments -> development -> Add environment secret):
  ADMIN_FUNCTION_APP   $ADM_APP
  ADMIN_PUBLIC_URL     https://$ADM_HOST
Then merge the admin-console pull request (or rerun "Deploy (development)").
Open https://$ADM_HOST/ and sign in as one of: $ADMINS
OUT
