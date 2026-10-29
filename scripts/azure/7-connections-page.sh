#!/usr/bin/env bash
# Step 7 (Azure Cloud Shell or a Mac with the Azure CLI): fill in the admin
# console's Connections page, which shows how to connect Claude, ChatGPT and
# Copilot Studio to the MCP server. Safe to re-run.
#
#   bash scripts/azure/7-connections-page.sh
#
# It looks up the search app's address and the client ID of each platform's app
# registration (created by 2-search-api-entra.sh) and writes them to the console
# as app settings. IDs and URLs only: no client secret is read, created or stored.
# Changing app settings restarts the console (a few seconds).
set -euo pipefail
source "$(dirname "$0")/common.sh"

az functionapp show -g "$RG" -n "$ADM_APP" >/dev/null 2>&1 \
  || { echo "ERROR: $ADM_APP is missing; run scripts/azure/4-admin-console.sh first" >&2; exit 1; }

client_id() {  # client_id <platform label>
  local id
  id="$(az ad app list --display-name "FS Communications MCP - $1" --query "[0].appId" -o tsv)"
  [ -n "$id" ] || { echo "ERROR: \"FS Communications MCP - $1\" not found; run scripts/azure/2-search-api-entra.sh first" >&2; exit 1; }
  echo "$id"
}
CLAUDE_ID="$(client_id Claude)"
CHATGPT_ID="$(client_id ChatGPT)"
COPILOT_ID="$(client_id "Copilot Studio")"
SRCH_URL="https://$(app_host "$SRCH_APP")"

az functionapp config appsettings set -g "$RG" -n "$ADM_APP" --only-show-errors >/dev/null --settings \
  "SEARCH_PUBLIC_URL=$SRCH_URL" "MCP_CLIENT_ID_CLAUDE=$CLAUDE_ID" \
  "MCP_CLIENT_ID_CHATGPT=$CHATGPT_ID" "MCP_CLIENT_ID_COPILOT=$COPILOT_ID"
echo "  MCP server        $SRCH_URL/mcp"
echo "  Claude            $CLAUDE_ID"
echo "  ChatGPT           $CHATGPT_ID"
echo "  Copilot Studio    $COPILOT_ID"
echo "Done. Open the console's Connections page (under Lab)."
