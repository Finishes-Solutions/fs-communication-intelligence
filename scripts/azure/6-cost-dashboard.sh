#!/usr/bin/env bash
# Step 6 (Azure Cloud Shell or a Mac with the Azure CLI, signed in as someone who
# can assign roles on the resource group): connect the admin console's Costs
# page to Azure's billed costs. Safe to re-run.
#
#   bash scripts/azure/6-cost-dashboard.sh
#
# What it does:
#  1. Gives the console's identity (id-fs-ci-admin-dev) the read-only role
#     "Cost Management Reader" on this resource group only. It can read cost
#     figures and nothing else; it cannot change or create anything.
#  2. Tells the console which subscription and resource group to read
#     (app settings AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP). The
#     subscription id stays in Azure, not in the repository.
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
role "$(mi "$ADM_MI" principalId)" "Cost Management Reader" "$RG_ID"

echo "== console settings"
az functionapp config appsettings set -g "$RG" -n "$ADM_APP" --only-show-errors >/dev/null --settings \
  "AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)" "AZURE_RESOURCE_GROUP=$RG"
echo "  $ADM_APP reads costs for resource group $RG"
echo "Done. Open the console's Costs page; billed figures can take up to 30 minutes to appear."
