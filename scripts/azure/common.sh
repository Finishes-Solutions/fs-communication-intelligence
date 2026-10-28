# Shared names for the dev environment. Sourced by the other scripts in this folder.
# Override any value by exporting it before running a script.
RG="${RG:-fs-communication-intelligence}"
LOC="${LOC:-centralus}"
SUFFIX="${SUFFIX:-1026}"
PY="${PY:-3.14}"
ING_APP="${ING_APP:-func-fs-ci-ing-dev-$SUFFIX}"
SRCH_APP="${SRCH_APP:-func-fs-ci-srch-dev-$SUFFIX}"
ING_ST="${ING_ST:-stfsciingdev$SUFFIX}"
SRCH_ST="${SRCH_ST:-stfscisrchdev$SUFFIX}"
ING_MI="${ING_MI:-id-fs-ci-ingestion-dev}"
SRCH_MI="${SRCH_MI:-id-fs-ci-search-dev}"
GH_MI="${GH_MI:-id-fs-ci-github-dev}"
PG="${PG:-psql-fs-ci-dev-1026b}"
AOAI="${AOAI:-aoai-fs-ci-dev-$SUFFIX}"
LANG_SVC="${LANG_SVC:-lang-fs-ci-dev-$SUFFIX}"
SAFE="${SAFE:-safe-fs-ci-dev-$SUFFIX}"
APPI="${APPI:-appi-fs-ci-dev}"
REPO="${REPO:-Finishes-Solutions/fs-communication-intelligence}"
API_NAME="${API_NAME:-FS Communications Search API}"
GRAPH_APP_ID=00000003-0000-0000-c000-000000000000

TENANT="$(az account show --query tenantId -o tsv)"
mi() { az identity show -g "$RG" -n "$1" --query "$2" -o tsv; }
endpoint() { az cognitiveservices account show -g "$RG" -n "$1" --query properties.endpoint -o tsv; }
role() {  # role <principal object id> <role name> <scope>
  az role assignment create --assignee-object-id "$1" --assignee-principal-type ServicePrincipal \
    --role "$2" --scope "$3" --only-show-errors >/dev/null
  echo "  role ok: $2"
}
uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
app_host() {  # app_host <function app>: its default host name; fails loudly if Azure returns none
  local h
  h="$(az functionapp show -g "$RG" -n "$1" \
    --query "defaultHostName || properties.defaultHostName || hostNames[0] || enabledHostNames[0]" \
    -o tsv 2>/dev/null)"
  if [ -z "$h" ] || [ "$h" = "None" ]; then
    h="$(az resource show -g "$RG" -n "$1" --resource-type Microsoft.Web/sites \
      --query "properties.defaultHostName" -o tsv 2>/dev/null)"
  fi
  if [ -z "$h" ] || [ "$h" = "None" ]; then
    echo "ERROR: could not read the host name of $1; see Function App -> Overview -> Default domain" >&2
    return 1
  fi
  echo "$h"
}
