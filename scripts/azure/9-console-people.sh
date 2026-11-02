#!/usr/bin/env bash
# Step 9 (Azure Cloud Shell or a Mac with the Azure CLI, signed in as an Entra
# admin): let the admin console's People page change who has access, acting as
# the signed-in admin. Safe to re-run.
#
#   ADMINS="svc_automation@finishessolutions.com matthews@finishessolutions.com codyv@finishessolutions.com" \
#     bash scripts/azure/9-console-people.sh
#   REMOVE=1 ADMINS="someone@finishessolutions.com" bash scripts/azure/9-console-people.sh
#
# What it does, on the "FS Communications Admin Console" sign-in app only:
#  1. Adds three DELEGATED Microsoft Graph permissions: AppRoleAssignment.ReadWrite.All
#     and Application.Read.All (list and change app role assignments) and
#     User.ReadBasic.All (find a person by email address).
#  2. Consents them for each person in ADMINS only (not tenant-wide), so nobody
#     else can get such a token through the console.
# The console's server and managed identity get nothing: the page calls Graph
# from the browser with the admin's own token, and Entra still refuses any change
# that admin could not make in the Entra portal (they need a role such as Cloud
# Application Administrator). Changes appear in Entra's audit log under their name.
set -euo pipefail
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }

: "${ADMINS:?set ADMINS to the console admins who may change access, space-separated}"
SPA_NAME="${SPA_NAME:-FS Communications Admin Console}"
GRAPH_APP=00000003-0000-0000-c000-000000000000
PERMS="AppRoleAssignment.ReadWrite.All Application.Read.All User.ReadBasic.All"

SPA_APP_ID="$(az ad app list --display-name "$SPA_NAME" --query "[0].appId" -o tsv)"
[ -n "$SPA_APP_ID" ] || { echo "ERROR: \"$SPA_NAME\" not found; run scripts/azure/4-admin-console.sh first" >&2; exit 1; }
SPA_SP="$(az ad sp list --filter "appId eq '$SPA_APP_ID'" --query "[0].id" -o tsv)"
[ -n "$SPA_SP" ] || SPA_SP="$(az ad sp create --id "$SPA_APP_ID" --query id -o tsv)"
GRAPH_SP="$(az ad sp list --filter "appId eq '$GRAPH_APP'" --query "[0].id" -o tsv)"

if [ "${REMOVE:-0}" != "1" ]; then
  echo "== delegated Graph permissions on \"$SPA_NAME\""
  for p in $PERMS; do
    id="$(az ad sp show --id "$GRAPH_APP" --query "oauth2PermissionScopes[?value=='$p'].id | [0]" -o tsv)"
    az ad app permission add --id "$SPA_APP_ID" --api "$GRAPH_APP" --api-permissions "$id=Scope" \
      --only-show-errors >/dev/null 2>&1 || true
    echo "  $p"
  done
fi

echo "== consent for: $ADMINS"
for upn in $ADMINS; do
  oid="$(az ad user show --id "$upn" --query id -o tsv 2>/dev/null)" \
    || { echo "ERROR: no Entra user $upn (check the spelling)" >&2; exit 1; }
  grant="$(graph GET "oauth2PermissionGrants?\$filter=clientId eq '$SPA_SP' and principalId eq '$oid' and resourceId eq '$GRAPH_SP'" \
    | python3 -c 'import json,sys; v=json.load(sys.stdin)["value"]; print((v[0]["id"]+" "+v[0]["scope"]) if v else "")')"
  gid="${grant%% *}"; have="${grant#* }"; [ -n "$grant" ] || have=""
  scope="$(HAVE="$have" PERMS="$PERMS" REMOVE="${REMOVE:-0}" python3 -c '
import os
have = os.environ["HAVE"].split()
perms = os.environ["PERMS"].split()
if os.environ["REMOVE"] == "1":
    keep = [s for s in have if s not in perms]
else:
    keep = have + [s for s in ["openid", "profile", *perms] if s not in have]
print(" ".join(keep))')"
  if [ -n "$gid" ] && [ -z "$scope" ]; then
    graph DELETE "oauth2PermissionGrants/$gid" >/dev/null
  elif [ -n "$gid" ]; then
    graph PATCH "oauth2PermissionGrants/$gid" "{\"scope\":\"$scope\"}" >/dev/null
  elif [ "${REMOVE:-0}" != "1" ]; then
    graph POST oauth2PermissionGrants "{\"clientId\":\"$SPA_SP\",\"consentType\":\"Principal\",\"principalId\":\"$oid\",\"resourceId\":\"$GRAPH_SP\",\"scope\":\"$scope\"}" >/dev/null
  fi
  if [ "${REMOVE:-0}" = "1" ]; then echo "  $upn: removed"; else echo "  $upn: consented"; fi
done
echo "Done. In the console, open People and choose Connect to Microsoft Entra."
echo "Changing access also needs an Entra role such as Cloud Application Administrator."
