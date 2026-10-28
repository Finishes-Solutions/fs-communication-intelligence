#!/usr/bin/env bash
# Who may use the system (Azure Cloud Shell or a Mac with the Azure CLI, signed
# in as someone who can assign app roles). Safe to re-run.
#
#   bash scripts/azure/people.sh list
#   bash scripts/azure/people.sh add someone@finishessolutions.com            # console + search
#   bash scripts/azure/people.sh add someone@finishessolutions.com console    # admin console only
#   bash scripts/azure/people.sh add someone@finishessolutions.com search     # Claude/ChatGPT/Copilot only
#   bash scripts/azure/people.sh remove someone@finishessolutions.com         # both
#
# Both are app roles on the "FS Communications Search API" registration:
#   console = Communications.Admin: the admin console, which shows ALL captured
#             mail (every mailbox), schedule, scope, costs and settings.
#   search  = Communications.Reader.All: searching every enrolled mailbox from
#             Claude, ChatGPT or Copilot.
# Each grant is a decision about who may read everyone's mail; keep the list short
# and review it (bash scripts/azure/people.sh list).
set -euo pipefail
source "$(dirname "$0")/common.sh"
graph() { az rest --method "$1" --uri "https://graph.microsoft.com/v1.0/$2" \
  --headers Content-Type=application/json ${3:+--body "$3"} --only-show-errors; }

action="${1:-list}"; upn="${2:-}"; which="${3:-both}"
API_APP_ID="$(az ad app list --display-name "$API_NAME" --query "[0].appId" -o tsv)"
[ -n "$API_APP_ID" ] || { echo "ERROR: \"$API_NAME\" not found; run scripts/azure/2-search-api-entra.sh first" >&2; exit 1; }
API_SP="$(az ad sp list --filter "appId eq '$API_APP_ID'" --query "[0].id" -o tsv)"
role_id() { az ad app show --id "$API_APP_ID" --query "appRoles[?value=='$1'].id | [0]" -o tsv; }
ADMIN_ROLE="$(role_id Communications.Admin)"
READER_ROLE="$(role_id Communications.Reader.All)"

if [ "$action" = "list" ]; then
  graph GET "servicePrincipals/$API_SP/appRoleAssignedTo?\$top=999" | ADMIN_ROLE="$ADMIN_ROLE" READER_ROLE="$READER_ROLE" python3 -c '
import json, os, sys
names = {os.environ["ADMIN_ROLE"]: "console (Communications.Admin)",
         os.environ["READER_ROLE"]: "search (Communications.Reader.All)"}
rows = sorted((a["principalDisplayName"], names.get(a["appRoleId"], a["appRoleId"]))
              for a in json.load(sys.stdin)["value"])
for who, role in rows:
    print(f"  {who:<40} {role}")
print(f"{len(rows)} assignment(s)")'
  exit 0
fi

[ -n "$upn" ] || { echo "usage: people.sh add|remove someone@finishessolutions.com [console|search|both]" >&2; exit 2; }
case "$which" in
  console) roles="$ADMIN_ROLE" ;;
  search) roles="$READER_ROLE" ;;
  both) roles="$ADMIN_ROLE $READER_ROLE" ;;
  *) echo "the third argument is console, search or both" >&2; exit 2 ;;
esac
oid="$(az ad user show --id "$upn" --query id -o tsv 2>/dev/null)" \
  || { echo "ERROR: no Entra user $upn (check the spelling)" >&2; exit 1; }
existing="$(graph GET "users/$oid/appRoleAssignments" | API_SP="$API_SP" python3 -c '
import json, os, sys
for a in json.load(sys.stdin)["value"]:
    if a["resourceId"] == os.environ["API_SP"]:
        print(a["appRoleId"], a["id"])')"

for role in $roles; do
  label="$([ "$role" = "$ADMIN_ROLE" ] && echo console || echo search)"
  assignment="$(echo "$existing" | awk -v r="$role" '$1 == r {print $2}')"
  if [ "$action" = "add" ]; then
    if [ -n "$assignment" ]; then echo "  $upn already has $label"; continue; fi
    graph POST "servicePrincipals/$API_SP/appRoleAssignedTo" \
      "{\"principalId\":\"$oid\",\"resourceId\":\"$API_SP\",\"appRoleId\":\"$role\"}" >/dev/null
    echo "  $upn: $label granted"
  elif [ "$action" = "remove" ]; then
    if [ -z "$assignment" ]; then echo "  $upn does not have $label"; continue; fi
    graph DELETE "servicePrincipals/$API_SP/appRoleAssignedTo/$assignment" >/dev/null
    echo "  $upn: $label removed"
  else
    echo "unknown action $action (list, add or remove)" >&2; exit 2
  fi
done
echo "Changes apply at the person's next sign-in (sign out and back in, up to an hour for an open session)."
