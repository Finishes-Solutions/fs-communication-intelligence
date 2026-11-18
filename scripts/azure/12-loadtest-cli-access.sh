#!/usr/bin/env bash
# Let the Azure CLI get a token for the search API, so the MCP load test
# (scripts/loadtest/mcp_load.py) can run as the signed-in account
# (Matthew, 2026-11-18). Run as svc_automation@ (Cloud Shell or a Mac with the
# Azure CLI). Safe to re-run.
#
#   bash scripts/azure/12-loadtest-cli-access.sh             # allow
#   REMOVE=1 bash scripts/azure/12-loadtest-cli-access.sh    # undo
#
# What it changes: the "FS Communications Search API" app registration lists the
# Azure CLI (Microsoft's own client, 04b07795-8ddb-461a-bbee-02f9e1bf7b46) as
# pre-authorized for the delegated scope Communications.Search, so the CLI can ask
# for that scope without a consent prompt. Nothing else changes: "assignment
# required" still decides who may sign in, the token still carries the person's
# own roles, and the server applies the same grants and reader limits. Any person
# already assigned to the API could get the same access through Claude or
# ChatGPT; this only adds the command line as a way in. Undo it after testing if
# you prefer.
set -euo pipefail
source "$(dirname "$0")/common.sh"
CLI_APP=04b07795-8ddb-461a-bbee-02f9e1bf7b46

API_APP_ID="$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)"
[ -n "$API_APP_ID" ] || { echo "ERROR: app registration '$API_NAME' not found" >&2; exit 1; }
OBJ="$(az ad app show --id "$API_APP_ID" --query id -o tsv)"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
az rest --method GET --uri "https://graph.microsoft.com/v1.0/applications/$OBJ?\$select=api" \
  --only-show-errors > "$work/app.json"
# The whole api object is written back (a partial one would replace it), with only
# the pre-authorized list changed.
python3 - "$work/app.json" "$work/body.json" "$CLI_APP" "${REMOVE:-0}" <<'PY'
import json, sys
src, dst, cli, remove = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
api = json.load(open(src))["api"]
scope = next(s["id"] for s in api["oauth2PermissionScopes"] if s["value"] == "Communications.Search")
pre = [p for p in api.get("preAuthorizedApplications") or [] if p["appId"] != cli]
if not remove:
    pre.append({"appId": cli, "delegatedPermissionIds": [scope]})
api["preAuthorizedApplications"] = pre
json.dump({"api": api}, open(dst, "w"))
print("removing" if remove else "allowing", "Azure CLI for Communications.Search")
PY
az rest --method PATCH --uri "https://graph.microsoft.com/v1.0/applications/$OBJ" \
  --headers Content-Type=application/json --body "@$work/body.json" --only-show-errors >/dev/null
echo "Done. Check: az account get-access-token --scope api://$API_APP_ID/Communications.Search --query expiresOn -o tsv"
