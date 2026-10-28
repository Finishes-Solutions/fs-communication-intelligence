#!/usr/bin/env bash
# Step 1 (Azure Cloud Shell, Bash): create the two Flex Consumption Function Apps
# on the existing identities and storage accounts, give each identity only the
# Azure access it needs, open PostgreSQL to Azure services, and connect GitHub.
# Safe to re-run. Does not deploy code (GitHub Actions does) and does not turn
# capture on (CAPTURE_ENABLED=false, PROCESSING_ENABLED=false).
#
#   CHAT_DEPLOYMENT=gpt-4.1-mini bash scripts/azure/1-function-apps.sh
set -euo pipefail
: "${CHAT_DEPLOYMENT:?set CHAT_DEPLOYMENT to the name of your Azure OpenAI chat deployment}"
source "$(dirname "$0")/common.sh"

for app in "$ING_APP:$ING_ST:$ING_MI" "$SRCH_APP:$SRCH_ST:$SRCH_MI"; do
  IFS=: read -r name storage identity <<<"$app"
  mi_id="$(mi "$identity" id)"; mi_principal="$(mi "$identity" principalId)"
  storage_id="$(az storage account show -g "$RG" -n "$storage" --query id -o tsv)"
  echo "== $name (identity $identity, storage $storage)"
  # Host and deployment storage use the identity, so grant it before creating the app.
  for r in "Storage Blob Data Owner" "Storage Queue Data Contributor" "Storage Table Data Contributor"; do
    role "$mi_principal" "$r" "$storage_id"
  done
  if az functionapp show -g "$RG" -n "$name" >/dev/null 2>&1; then
    echo "  app exists"
  else
    az functionapp create -g "$RG" -n "$name" --flexconsumption-location "$LOC" \
      --runtime python --runtime-version "$PY" --storage-account "$storage" \
      --deployment-storage-auth-type UserAssignedIdentity --deployment-storage-auth-value "$mi_id" \
      --app-insights "$APPI" --only-show-errors >/dev/null
    echo "  app created"
  fi
  az functionapp identity assign -g "$RG" -n "$name" --identities "$mi_id" --only-show-errors >/dev/null
done

PGHOST="$PG.postgres.database.azure.com"
AOAI_EP="$(endpoint "$AOAI")"; LANG_EP="$(endpoint "$LANG_SVC")"; SAFE_EP="$(endpoint "$SAFE")"
ING_CLIENT="$(mi "$ING_MI" clientId)"; SRCH_CLIENT="$(mi "$SRCH_MI" clientId)"
SRCH_HOST="$(app_host "$SRCH_APP")"

echo "== app settings"
az functionapp config appsettings set -g "$RG" -n "$ING_APP" --only-show-errors >/dev/null --settings \
  "AzureWebJobsStorage__accountName=$ING_ST" "AzureWebJobsStorage__credential=managedidentity" \
  "AzureWebJobsStorage__clientId=$ING_CLIENT" "AZURE_CLIENT_ID=$ING_CLIENT" \
  "DEPLOYMENT_ENVIRONMENT=development" "DATABASE_AUTH=entra" "PGHOST=$PGHOST" "PGPORT=5432" \
  "PGUSER=$ING_MI" "PGDATABASE=ci" "PGSSLMODE=require" \
  "GRAPH_BACKEND=graph" "GRAPH_AUTH=managed_identity" "GRAPH_TENANT_ID=$TENANT" \
  "AZURE_OPENAI_ENDPOINT=$AOAI_EP" "AZURE_OPENAI_CHAT_DEPLOYMENT=$CHAT_DEPLOYMENT" \
  "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large" \
  "AZURE_LANGUAGE_ENDPOINT=$LANG_EP" "AZURE_CONTENT_SAFETY_ENDPOINT=$SAFE_EP" \
  "ATTACHMENT_EXTRACTION_ENABLED=true" "ATTACHMENT_UNSUPPORTED_POLICY=publish_partial" \
  "EMAIL_INCLUDE_SENT=true" "CAPTURE_ENABLED=false" "CAPTURE_HOURS=24" \
  "CAPTURE_SCHEDULE=0 0 0 1 1 *" "PROCESSING_ENABLED=false" "PROCESS_SCHEDULE=0 */5 * * * *"
az functionapp config appsettings set -g "$RG" -n "$SRCH_APP" --only-show-errors >/dev/null --settings \
  "AzureWebJobsStorage__accountName=$SRCH_ST" "AzureWebJobsStorage__credential=managedidentity" \
  "AzureWebJobsStorage__clientId=$SRCH_CLIENT" "AZURE_CLIENT_ID=$SRCH_CLIENT" \
  "DEPLOYMENT_ENVIRONMENT=development" "DATABASE_AUTH=entra" "PGHOST=$PGHOST" "PGPORT=5432" \
  "PGUSER=$SRCH_MI" "PGDATABASE=ci" "PGSSLMODE=require" \
  "AZURE_OPENAI_ENDPOINT=$AOAI_EP" "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large" \
  "ENTRA_TENANT_ID=$TENANT" "SEARCH_PUBLIC_URL=https://$SRCH_HOST"
# Host storage now uses the identity; drop the key-based connection string if one was created.
for name in "$ING_APP" "$SRCH_APP"; do
  az functionapp config appsettings delete -g "$RG" -n "$name" --setting-names AzureWebJobsStorage \
    --only-show-errors >/dev/null || true
done
echo "  settings ok"

echo "== AI service access (Entra, no keys)"
cog() { az cognitiveservices account show -g "$RG" -n "$1" --query id -o tsv; }
ING_P="$(mi "$ING_MI" principalId)"; SRCH_P="$(mi "$SRCH_MI" principalId)"
role "$ING_P" "Cognitive Services OpenAI User" "$(cog "$AOAI")"
role "$ING_P" "Cognitive Services User" "$(cog "$LANG_SVC")"
role "$ING_P" "Cognitive Services User" "$(cog "$SAFE")"
role "$SRCH_P" "Cognitive Services OpenAI User" "$(cog "$AOAI")"   # query embeddings only

echo "== PostgreSQL: allow Azure services (Entra sign-in is still required)"
az postgres flexible-server firewall-rule create -g "$RG" --server-name "$PG" --name AllowAzureServices \
  --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0 --only-show-errors >/dev/null
echo "  firewall ok"

echo "== GitHub Actions deploy identity"
GH_P="$(mi "$GH_MI" principalId)"
for name in "$ING_APP" "$SRCH_APP"; do
  role "$GH_P" "Website Contributor" "$(az functionapp show -g "$RG" -n "$name" --query id -o tsv)"
done
SUBJECT="repo:$REPO:environment:development"
if ! az identity federated-credential list -g "$RG" --identity-name "$GH_MI" \
     --query "[?subject=='$SUBJECT']" -o tsv | grep -q .; then
  az identity federated-credential create -g "$RG" --identity-name "$GH_MI" -n github-development \
    --issuer https://token.actions.githubusercontent.com --subject "$SUBJECT" \
    --audiences api://AzureADTokenExchange --only-show-errors >/dev/null
fi
echo "  federation ok ($SUBJECT)"

cat <<OUT

Done. Add these as SECRETS of the GitHub environment "development"
(GitHub -> repo Settings -> Environments -> development -> Add environment secret):
  AZURE_CLIENT_ID         $(mi "$GH_MI" clientId)
  AZURE_TENANT_ID         $TENANT
  AZURE_SUBSCRIPTION_ID   $(az account show --query id -o tsv)
  INGESTION_FUNCTION_APP  $ING_APP
  SEARCH_FUNCTION_APP     $SRCH_APP
  SEARCH_PUBLIC_URL       https://$SRCH_HOST
Next: bash scripts/azure/2-search-api-entra.sh
OUT
