#!/usr/bin/env bash
# Who may use the system (Azure Cloud Shell or a Mac with the Azure CLI, signed
# in as someone who can assign app roles). Safe to re-run.
#
#   bash scripts/azure/people.sh list
#   bash scripts/azure/people.sh add someone@finishessolutions.com            # console + search
#   bash scripts/azure/people.sh add someone@finishessolutions.com console    # admin console only
#   bash scripts/azure/people.sh add someone@finishessolutions.com search     # Claude/ChatGPT/Copilot only
#   bash scripts/azure/people.sh add someone@finishessolutions.com files      # open email/file links, no console
#   bash scripts/azure/people.sh remove someone@finishessolutions.com         # console + search
#   bash scripts/azure/people.sh remove someone@finishessolutions.com files
#
# All are app roles on the "FS Communications Search API" registration:
#   console = Communications.Admin: the admin console, which shows ALL captured
#             mail (every mailbox), schedule, scope, costs and settings.
#   search  = Communications.Reader.All: searching every enrolled mailbox from
#             Claude, ChatGPT or Copilot.
#   files   = Communications.Files: opening the read-only email page that links in
#             MCP results and on monday boards point to, and downloading its files,
#             without the console. Which emails open still follows the person's
#             search role (with search, every enrolled mailbox).
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
FILES_ROLE="$(role_id Communications.Files)"
if [ -z "$FILES_ROLE" ] && [ "$action" = "add" ] && [ "${3:-}" = "files" ]; then
  # First use: add the role to the registration. Graph replaces appRoles whole on
  # PATCH, so the existing roles are sent back with it.
  API_OBJ="$(az ad app show --id "$API_APP_ID" --query id -o tsv)"
  graph PATCH "applications/$API_OBJ" "$(graph GET "applications/$API_OBJ?\$select=appRoles" | python3 -c '
import json, sys, uuid
roles = json.load(sys.stdin)["appRoles"]
roles.append({"id": str(uuid.uuid4()), "value": "Communications.Files",
    "allowedMemberTypes": ["User"], "displayName": "Communications Files (open links)",
    "description": "Open emails and download their files from links, without the admin console.",
    "isEnabled": True})
print(json.dumps({"appRoles": roles}))')" >/dev/null
  echo "  added the Communications.Files role"
  FILES_ROLE="$(role_id Communications.Files)"
fi

if [ "$action" = "list" ]; then
  graph GET "servicePrincipals/$API_SP/appRoleAssignedTo?\$top=999" | ADMIN_ROLE="$ADMIN_ROLE" READER_ROLE="$READER_ROLE" FILES_ROLE="$FILES_ROLE" python3 -c '
import json, os, sys
names = {os.environ["ADMIN_ROLE"]: "console (Communications.Admin)",
         os.environ["READER_ROLE"]: "search (Communications.Reader.All)",
         os.environ["FILES_ROLE"] or "-": "files (Communications.Files)"}
rows = sorted((a["principalDisplayName"], names.get(a["appRoleId"], a["appRoleId"]))
              for a in json.load(sys.stdin)["value"])
for who, role in rows:
    print(f"  {who:<40} {role}")
print(f"{len(rows)} assignment(s)")'
  exit 0
fi

[ -n "$upn" ] || { echo "usage: people.sh add|remove someone@finishessolutions.com [console|search|files|both]" >&2; exit 2; }
case "$which" in
  console) roles="$ADMIN_ROLE" ;;
  search) roles="$READER_ROLE" ;;
  files) roles="$FILES_ROLE"
    [ -n "$roles" ] || { echo "  nobody has the files role yet (it is created by the first add)"; exit 0; } ;;
  both) roles="$ADMIN_ROLE $READER_ROLE" ;;
  *) echo "the third argument is console, search, files or both" >&2; exit 2 ;;
esac
oid="$(az ad user show --id "$upn" --query id -o tsv 2>/dev/null)" \
  || { echo "ERROR: no Entra user $upn (check the spelling)" >&2; exit 1; }
existing="$(graph GET "users/$oid/appRoleAssignments" | API_SP="$API_SP" python3 -c '
import json, os, sys
for a in json.load(sys.stdin)["value"]:
    if a["resourceId"] == os.environ["API_SP"]:
        print(a["appRoleId"], a["id"])')"

for role in $roles; do
  label="$(case "$role" in "$ADMIN_ROLE") echo console ;; "$FILES_ROLE") echo files ;; *) echo search ;; esac)"
  assignment="$(echo "$existing" | awk -v r="$role" '$1 == r {print $2}')"
  if [ "$action" = "add" ]; then
    if [ -n "$assignment" ]; then echo "  $upn already has $label"; continue; fi
    # A role added moments ago can take a little while to reach the service principal.
    for attempt in 1 2 3 4 5 6; do
      graph POST "servicePrincipals/$API_SP/appRoleAssignedTo" \
        "{\"principalId\":\"$oid\",\"resourceId\":\"$API_SP\",\"appRoleId\":\"$role\"}" \
        >/dev/null 2>&1 && break
      [ "$attempt" = 6 ] && { echo "ERROR: could not grant $label to $upn; rerun in a few minutes" >&2; exit 1; }
      sleep 10
    done
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
