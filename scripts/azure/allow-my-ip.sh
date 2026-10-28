#!/usr/bin/env bash
# Let this computer reach the dev PostgreSQL server (needed for db/migrate.py,
# psql and the ci command). Run on each computer or network you work from:
#
#   bash scripts/azure/allow-my-ip.sh matthew-work
#   bash scripts/azure/allow-my-ip.sh matthew-work --remove     # take it away again
#   bash scripts/azure/allow-my-ip.sh --list                    # who is allowed in
#
# The server holds real mail (CLAUDE.md section 3a): keep the list to the
# addresses you use and remove old ones. Sign-in with Entra is still required.
set -euo pipefail
source "$(dirname "$0")/common.sh"

if [ "${1:-}" = "--list" ]; then
  az postgres flexible-server firewall-rule list -g "$RG" --server-name "$PG" -o table 2>/dev/null \
    || az postgres flexible-server firewall-rule list -g "$RG" --name "$PG" -o table
  exit 0
fi
rule="${1:?give the rule a name, e.g. matthew-work}"
if [ "${2:-}" = "--remove" ]; then
  fw_rule delete "$rule" --yes
  echo "removed $rule"
  exit 0
fi
ip="$(curl -s https://api.ipify.org || true)"
[ -n "$ip" ] || { echo "ERROR: could not read this computer's public IP address" >&2; exit 1; }
fw_rule create "$rule" --start-ip-address "$ip" --end-ip-address "$ip"
echo "allowed $ip as $rule on $PG"
