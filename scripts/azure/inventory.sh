#!/usr/bin/env bash
# Read-only: what is deployed in the resource group, at which size and tier,
# the settings that drive cost, and (when the subscription allows it) the
# cost so far this month per resource. Changes nothing.
#
#   bash scripts/azure/inventory.sh
set -uo pipefail
RG="${RG:-fs-communication-intelligence}"
az config set extension.use_dynamic_install=yes_without_prompt --only-show-errors >/dev/null 2>&1
flag() { printf '  ! %s\n' "$*"; }

echo "== Everything in $RG"
az resource list -g "$RG" --query "sort_by([].{name:name, type:type, sku:sku.name}, &type)" -o table

echo
echo "== PostgreSQL (usually the biggest fixed cost)"
for s in $(az postgres flexible-server list -g "$RG" --query "[].name" -o tsv); do
  az postgres flexible-server show -g "$RG" -n "$s" --query "{server:name, tier:sku.tier, size:sku.name,
    storageGB:storage.storageSizeGb, storageTier:storage.tier, autoGrow:storage.autoGrow,
    highAvailability:highAvailability.mode, backupDays:backup.backupRetentionDays,
    geoBackup:backup.geoRedundantBackup, state:state}" -o table
  ha="$(az postgres flexible-server show -g "$RG" -n "$s" --query highAvailability.mode -o tsv)"
  tier="$(az postgres flexible-server show -g "$RG" -n "$s" --query sku.tier -o tsv)"
  geo="$(az postgres flexible-server show -g "$RG" -n "$s" --query backup.geoRedundantBackup -o tsv)"
  [ "$ha" != "Disabled" ] && flag "high availability is $ha: a standby server, roughly doubles compute; not needed for dev"
  [ "$tier" != "Burstable" ] && flag "tier $tier: Burstable is the low-cost tier for dev"
  [ "$geo" = "Enabled" ] && flag "geo-redundant backup is on: extra backup storage cost"
done

echo
echo "== Function Apps (Flex Consumption: pay per use unless instances are kept always ready)"
for app in $(az functionapp list -g "$RG" --query "[].name" -o tsv); do
  az functionapp scale config show -g "$RG" -n "$app" \
    --query "{app:'$app', memoryMB:instanceMemoryMB, maxInstances:maximumInstanceCount, alwaysReady:length(alwaysReady || \`[]\`)}" \
    -o table 2>/dev/null || echo "  $app: not a Flex Consumption app"
  ready="$(az functionapp scale config show -g "$RG" -n "$app" --query "length(alwaysReady || \`[]\`)" -o tsv 2>/dev/null || echo 0)"
  [ "${ready:-0}" != "0" ] && flag "$app keeps instances always ready: billed even when idle"
done

echo
echo "== Storage accounts"
az storage account list -g "$RG" --query "[].{name:name, sku:sku.name, kind:kind, accessTier:accessTier}" -o table

echo
echo "== Azure AI services (S/S0 = pay per use; no monthly fee)"
az cognitiveservices account list -g "$RG" --query "[].{name:name, kind:kind, tier:sku.name}" -o table
for a in $(az cognitiveservices account list -g "$RG" --query "[?kind=='OpenAI'].name" -o tsv); do
  echo "  deployments in $a (capacity is a rate limit; you pay per token used, not for capacity):"
  az cognitiveservices account deployment list -g "$RG" -n "$a" \
    --query "[].{deployment:name, model:properties.model.name, type:sku.name, capacityKTPM:sku.capacity}" -o table
done

echo
echo "== Key Vault, monitoring"
az keyvault list -g "$RG" --query "[].{name:name, sku:properties.sku.name}" -o table
az monitor log-analytics workspace list -g "$RG" \
  --query "[].{workspace:name, sku:sku.name, retentionDays:retentionInDays, dailyCapGB:workspaceCapping.dailyQuotaGb}" -o table

echo
echo "== Cost so far this month by resource (Azure billing data; may lag up to a day)"
start="$(date -u +%Y-%m-01)"; end="$(date -u +%Y-%m-%d)"
if az consumption usage list --start-date "$start" --end-date "$end" -o json 2>/dev/null >/tmp/fsci-usage.json; then
  python3 - "$RG" <<'PY'
import json, sys
rg = "/resourcegroups/" + sys.argv[1].lower() + "/"
totals, currency = {}, ""
for row in json.load(open("/tmp/fsci-usage.json")):
    if rg not in (row.get("instanceId") or "").lower():
        continue
    name = row.get("instanceName") or "?"
    totals[name] = totals.get(name, 0.0) + float(row.get("pretaxCost") or 0)
    currency = row.get("currency") or currency
for name, cost in sorted(totals.items(), key=lambda kv: -kv[1]):
    print(f"  {cost:10.2f} {currency}  {name}")
print(f"  {sum(totals.values()):10.2f} {currency}  TOTAL month to date")
PY
  rm -f /tmp/fsci-usage.json
else
  echo "  not available for this subscription type; see Azure portal -> Cost Management -> Cost analysis,"
  echo "  filtered to resource group $RG and grouped by resource."
fi
