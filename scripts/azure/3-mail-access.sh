#!/usr/bin/env bash
# Step 3 (Azure Cloud Shell, Bash; needs a Privileged Role / Application admin):
# give the ingestion managed identity Microsoft Graph User.Read.All (list users)
# and print the Exchange Online commands that grant read-only mail access to
# all user and shared mailboxes except those tagged FS-CI-Exclude.
# Never grant Entra Mail.Read: Exchange RBAC is the mailbox boundary (L5).
set -euo pipefail
source "$(dirname "$0")/common.sh"

ING_P="$(mi "$ING_MI" principalId)"; ING_CLIENT="$(mi "$ING_MI" clientId)"
GRAPH_SP="$(az ad sp show --id "$GRAPH_APP_ID" --query id -o tsv)"
ROLE="$(az ad sp show --id "$GRAPH_APP_ID" --query "appRoles[?value=='User.Read.All'].id | [0]" -o tsv)"
if az rest --method GET --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$ING_P/appRoleAssignments" \
     --query "value[?appRoleId=='$ROLE'] | length(@)" -o tsv | grep -qx 0; then
  az rest --method POST --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$ING_P/appRoleAssignments" \
    --headers Content-Type=application/json --only-show-errors >/dev/null \
    --body "{\"principalId\":\"$ING_P\",\"resourceId\":\"$GRAPH_SP\",\"appRoleId\":\"$ROLE\"}"
fi
echo "Graph User.Read.All -> $ING_MI: ok"

cat <<PS

Now run these in Cloud Shell -> PowerShell (or any PowerShell with ExchangeOnlineManagement),
signed in as an Exchange administrator:

Connect-ExchangeOnline
# Tag every mailbox that must stay out FIRST, e.g.:
# Set-Mailbox -Identity 'someone@finishessolutions.com' -CustomAttribute15 'FS-CI-Exclude'
New-ServicePrincipal -AppId '$ING_CLIENT' -ObjectId '$ING_P' -DisplayName 'FS CI Email Ingestion Dev'
\$Filter = "(RecipientTypeDetails -eq 'UserMailbox' -or RecipientTypeDetails -eq 'SharedMailbox') -and -not (CustomAttribute15 -eq 'FS-CI-Exclude')"
if (-not (Get-ManagementScope -Identity 'FS-CI-Email-All' -ErrorAction SilentlyContinue)) {
  New-ManagementScope -Name 'FS-CI-Email-All' -RecipientRestrictionFilter \$Filter }
New-ManagementRoleAssignment -Name 'FS-CI-Email-All-Read-Dev' -App '$ING_P' -Role 'Application Mail.Read' -CustomResourceScope 'FS-CI-Email-All'
Test-ServicePrincipalAuthorization -Identity '$ING_P' -Resource 'chuck@finishessolutions.com'

Expect InScope = True. Exchange can take up to about 2 hours to apply it.
PS
