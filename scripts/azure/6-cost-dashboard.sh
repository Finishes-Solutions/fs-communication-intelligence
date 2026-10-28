#!/usr/bin/env bash
# Step 6 (Azure Cloud Shell or a Mac with the Azure CLI, signed in as someone who
# can assign roles on the resource group): connect the admin console's Costs
# page to Azure's billed costs and to the list of AI models. Safe to re-run.
#
#   bash scripts/azure/6-cost-dashboard.sh
#
# What it does:
#  1. Gives the console's identity (id-fs-ci-admin-dev) the read-only role
#     "Cost Management Reader" on this resource group only. It can read cost
#     figures and nothing else; it cannot change or create anything.
#  2. Gives the same identity the read-only "Reader" role on the Azure OpenAI
#     account only, so the console can list its model deployments (choosing a
#     model; creating deployments stays in the Azure portal).
#  3. Tells the console which subscription, resource group and Azure OpenAI
#     account to read (app settings AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP,
#     AZURE_OPENAI_ACCOUNT). The subscription id stays in Azure, not in the repository.
# A new role can take up to 30 minutes to apply; until then the page says so.
# The metered estimate needs database migration 0013 (db/migrate.py apply).
set -euo pipefail
source "$(dirname "$0")/common.sh"

az identity show -g "$RG" -n "$ADM_MI" >/dev/null 2>&1 \
  || { echo "ERROR: $ADM_MI is missing; run scripts/azure/4-admin-console.sh first" >&2; exit 1; }
az functionapp show -g "$RG" -n "$ADM_APP" >/dev/null 2>&1 \
  || { echo "ERROR: $ADM_APP is missing; run scripts/azure/4-admin-console.sh first" >&2; exit 1; }

echo "== read-only cost access for the console"
RG_ID="$(az group show -n "$RG" --query id -o tsv)"
ADM_P="$(mi "$ADM_MI" principalId)"
role "$ADM_P" "Cost Management Reader" "$RG_ID"

echo "== read-only model list for the console"
AOAI_ID="$(az cognitiveservices account show -g "$RG" -n "$AOAI" --query id -o tsv)"
role "$ADM_P" "Reader" "$AOAI_ID"

echo "== console settings"
az functionapp config appsettings set -g "$RG" -n "$ADM_APP" --only-show-errors >/dev/null --settings \
  "AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)" "AZURE_RESOURCE_GROUP=$RG" \
  "AZURE_OPENAI_ACCOUNT=$AOAI"
echo "  $ADM_APP reads costs for resource group $RG and models on $AOAI"
echo "Done. Open the console's Costs page; billed figures can take up to 30 minutes to appear."
