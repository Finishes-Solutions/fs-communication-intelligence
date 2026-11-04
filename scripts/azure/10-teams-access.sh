#!/usr/bin/env bash
# Step 10 (Azure Cloud Shell, Bash, signed in as svc_automation; needs a Privileged
# Role or Application administrator for the Graph grants):
# T0 of docs/TEAMS_PLAN.md. Creates the Teams identity, attaches it to the
# ingestion Function App as a second identity, grants it the chats-only Graph
# application permissions (D-T1 option A, D-T2 channels off), stores its client
# id as an app setting, and prints the command that runs the access probe.
#
# The mail identity (id-fs-ci-ingestion-dev) is not touched: it keeps no
# tenant-wide Graph read (L5). The Teams identity gets no mailbox, Blob, Key
# Vault or database access.
set -euo pipefail
source "$(dirname "$0")/common.sh"
TEAMS_MI="${TEAMS_MI:-id-fs-ci-teams-dev}"
ROLES=(User.Read.All Chat.Read.All ChatMember.Read.All)
if [ "${TEAMS_CHANNELS:-false}" = "true" ]; then  # only once channels are turned on (D-T2)
  ROLES+=(ChannelMessage.Read.All Team.ReadBasic.All Channel.ReadBasic.All)
fi

echo "== identity $TEAMS_MI"
if ! az identity show -g "$RG" -n "$TEAMS_MI" --only-show-errors >/dev/null 2>&1; then
  az identity create -g "$RG" -n "$TEAMS_MI" -l "$LOC" --only-show-errors >/dev/null
  echo "  created (Entra needs a minute before it can be granted roles)"; sleep 30
fi
MI_ID="$(mi "$TEAMS_MI" id)"; MI_P="$(mi "$TEAMS_MI" principalId)"; MI_CLIENT="$(mi "$TEAMS_MI" clientId)"
ING_MI_ID="$(mi "$ING_MI" id)"

echo "== attach to $ING_APP (keeps $ING_MI)"
az functionapp identity assign -g "$RG" -n "$ING_APP" --identities "$ING_MI_ID" "$MI_ID" \
  --only-show-errors >/dev/null
az functionapp config appsettings set -g "$RG" -n "$ING_APP" --only-show-errors >/dev/null \
  --settings "AZURE_TEAMS_CLIENT_ID=$MI_CLIENT" "TEAMS_CAPTURE_ENABLED=false"
echo "  app setting AZURE_TEAMS_CLIENT_ID set; TEAMS_CAPTURE_ENABLED=false"

echo "== Graph application permissions (app role assignments on the Graph service principal)"
GRAPH_SP="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
for role in "${ROLES[@]}"; do
  ROLE_ID="$(az ad sp show --id "$GRAPH_APP_ID" --query "appRoles[?value=='$role'].id | [0]" -o tsv)"
  if [ -z "$ROLE_ID" ] || [ "$ROLE_ID" = "None" ]; then echo "  ERROR: Graph has no app role $role" >&2; exit 1; fi
  if az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_P/appRoleAssignments" \
       --query "value[?appRoleId=='$ROLE_ID'] | length(@)" -o tsv | grep -qx 0; then
    az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$MI_P/appRoleAssignments" \
      --headers Content-Type=application/json --only-show-errors >/dev/null \
      --body "{\"principalId\":\"$MI_P\",\"resourceId\":\"$GRAPH_SP\",\"appRoleId\":\"$ROLE_ID\"}"
  fi
  echo "  $role -> $TEAMS_MI: ok"
done

HOST="$(app_host "$ING_APP")"
cat <<TXT

Done. The probe runs inside the ingestion app (it needs the identity's own token), so the
app must carry the code from the Teams branch first (merge, or Actions -> Deploy -> Run workflow).
Entra can take a few minutes to apply new app roles. Then, in this same shell:

  KEY=\$(az functionapp keys list -g $RG -n $ING_APP --query masterKey -o tsv)
  curl -s "https://$HOST/api/teams-probe?code=\$KEY&user=<someone>@finishessolutions.com" | python3 -c 'import json,sys; print(json.load(sys.stdin)["text"])'

Read the verdict:
  ok                               chat export works; T1 can start
  protected_api_approval_required  Microsoft wants the Protected APIs request form for this
                                   identity: search "Protected APIs in Microsoft Teams" on
                                   learn.microsoft.com, submit it, re-run after approval
  forbidden / unauthorized         roles missing or not applied yet; re-run in 10 minutes
The reply holds status codes, Graph error codes, counts and role names only; paste it into the
PR or chat as is. Record the outcome in docs/TEAMS_PLAN.md section 8 and ADR-022.
TXT
