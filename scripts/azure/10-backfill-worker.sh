#!/usr/bin/env bash
# A temporary processing worker for a backfill (Matthew, 2026-11-03): one Azure VM
# with the ingestion managed identity attached, running several `ci process`
# loops with attachment extraction on, next to the Function App. The identity is
# the same one the ingestion app uses, so Graph (Exchange RBAC), PostgreSQL,
# Azure OpenAI and Blob access are unchanged; nothing new is granted. No inbound
# ports are opened and nothing is stored on the VM but the code and logs.
#
#   bash scripts/azure/10-backfill-worker.sh            # create (or update) and start
#   bash scripts/azure/10-backfill-worker.sh --status   # processes and the last log lines
#   bash scripts/azure/10-backfill-worker.sh --stop     # delete the VM and its firewall rule
#
# Settings (export before running): SIZE (Standard_D16s_v5), PROCS (3 processes),
# WORKERS (64 per process), REF (git branch or tag the VM runs; default main),
# CHAT_DEPLOYMENT (gpt-4.1-mini; the console's choice still wins at run time).
# The VM costs about $0.80 an hour for D16s_v5: run --stop when the backlog is gone.
set -euo pipefail
source "$(dirname "$0")/common.sh"

VM="${VM:-vm-fs-ci-backfill}"
SIZE="${SIZE:-Standard_D16s_v5}"
PROCS="${PROCS:-3}"
WORKERS="${WORKERS:-64}"
REF="${REF:-main}"
CHAT_DEPLOYMENT="${CHAT_DEPLOYMENT:-gpt-4.1-mini}"
EMBED_TPM="${EMBED_TPM:-300000}"   # per process; keep PROCS x this below the deployment's quota
FW_RULE="backfill-worker"

run_on_vm() {  # run_on_vm <script text>: run as root on the VM, print its output
  az vm run-command invoke -g "$RG" -n "$VM" --command-id RunShellScript --scripts "$1" \
    --query "value[0].message" -o tsv
}

case "${1:-}" in
  --status)
    run_on_vm 'echo "processes: $(pgrep -fc "ci process" || true)"; for f in /opt/ci/proc*.log; do echo "== $f"; tail -n 4 "$f"; done'
    exit 0 ;;
  --stop)
    echo "== deleting $VM and its disks"
    az vm delete -g "$RG" -n "$VM" --yes --force-deletion yes --only-show-errors -o none || true
    for r in $(az resource list -g "$RG" --query "[?contains(name, '$VM')].id" -o tsv); do
      az resource delete --ids "$r" --only-show-errors -o none || true
    done
    fw_rule delete "$FW_RULE" --yes || true
    echo "deleted"
    exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--status|--stop]" >&2; exit 2 ;;
esac

mi_id="$(mi "$ING_MI" id)"; mi_client="$(mi "$ING_MI" clientId)"
echo "== VM $VM ($SIZE) with identity $ING_MI"
if az vm show -g "$RG" -n "$VM" >/dev/null 2>&1; then
  echo "  VM exists"
else
  az vm create -g "$RG" -n "$VM" --image Ubuntu2404 --size "$SIZE" --admin-username azureuser \
    --generate-ssh-keys --assign-identity "$mi_id" --public-ip-sku Standard --nsg-rule NONE \
    --os-disk-size-gb 64 --only-show-errors -o none
  echo "  VM created"
fi
ip="$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)"
[ -n "$ip" ] || { echo "ERROR: the VM has no public IP" >&2; exit 1; }
fw_rule delete "$FW_RULE" --yes 2>/dev/null || true
fw_rule create "$FW_RULE" --start-ip-address "$ip" --end-ip-address "$ip"
echo "  PostgreSQL allows $ip ($FW_RULE)"

aoai_ep="$(endpoint "$AOAI")"; lang_ep="$(endpoint "$LANG_SVC")"; safe_ep="$(endpoint "$SAFE")"
bootstrap=$(cat <<EOS
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null && apt-get install -y -q git curl ca-certificates >/dev/null
[ -x /root/.local/bin/uv ] || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
export PATH=/root/.local/bin:\$PATH
mkdir -p /opt/ci && cd /opt/ci
if [ ! -d repo ]; then git clone -q --depth 1 -b "$REF" "https://github.com/$REPO.git" repo; fi
cd repo && git fetch -q --depth 1 origin "$REF" && git checkout -q FETCH_HEAD
uv sync -q
cat > /opt/ci/env <<EOF
DEPLOYMENT_ENVIRONMENT=development
AZURE_MANAGED_IDENTITY=1
AZURE_CLIENT_ID=$mi_client
DATABASE_AUTH=entra
PGHOST=$PG.postgres.database.azure.com
PGPORT=5432
PGUSER=$ING_MI
PGDATABASE=ci
PGSSLMODE=require
GRAPH_BACKEND=graph
GRAPH_AUTH=managed_identity
GRAPH_TENANT_ID=$TENANT
AZURE_OPENAI_ENDPOINT=$aoai_ep
AZURE_OPENAI_CHAT_DEPLOYMENT=$CHAT_DEPLOYMENT
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large
AZURE_LANGUAGE_ENDPOINT=$lang_ep
AZURE_CONTENT_SAFETY_ENDPOINT=$safe_ep
ATTACHMENT_EXTRACTION_ENABLED=true
ATTACHMENT_UNSUPPORTED_POLICY=publish_partial
ATTACHMENT_STORE_ORIGINALS=true
EVIDENCE_BLOB_ACCOUNT=$EVI_ST
EVIDENCE_BLOB_CONTAINER=$EVI_CONTAINER
EMAIL_INCLUDE_SENT=true
EMBEDDING_TOKENS_PER_MINUTE=$EMBED_TPM
EOF
pkill -f "ci process" || true
sleep 2
for i in \$(seq 1 $PROCS); do
  setsid nohup bash -c "while true; do /root/.local/bin/uv run --env-file /opt/ci/env ci process --workers $WORKERS >> /opt/ci/proc\$i.log 2>&1; sleep 30; done" >/dev/null 2>&1 </dev/null &
done
sleep 5
echo "started \$(pgrep -fc 'ci process' || true) process loop(s) on $REF"
EOS
)
echo "== installing and starting $PROCS x $WORKERS workers (a few minutes)"
run_on_vm "$bootstrap"
echo
echo "Check on it:   bash scripts/azure/10-backfill-worker.sh --status"
echo "When done:     bash scripts/azure/10-backfill-worker.sh --stop"
