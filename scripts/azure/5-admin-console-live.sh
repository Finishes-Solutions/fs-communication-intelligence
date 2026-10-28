#!/usr/bin/env bash
# Put the admin console (web front end + API) live, start to finish, from Azure
# Cloud Shell in the repository folder:
#
#   bash scripts/azure/5-admin-console-live.sh
#
# 1. checks the tools and the Azure OpenAI chat deployment;
# 2. switches this clone to the admin-console branch (new scripts and migrations);
# 3. runs scripts/azure/4-admin-console.sh: Azure resources, sign-in, file
#    storage, database migrations and the console's database login;
# 4. stores the two GitHub environment secrets (GitHub CLI sign-in if needed);
# 5. merges the admin-console pull request, which deploys all three apps;
# 6. waits for the deploy and checks that the console answers;
# 7. switches this clone back to main.
# Order matters: the database changes must exist before the new code deploys.
# Safe to re-run. Defaults: CHAT_DEPLOYMENT=gpt-4.1-mini,
# ADMINS=matthews@finishessolutions.com (export to change).
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO_SLUG="Finishes-Solutions/fs-communication-intelligence"
BRANCH="${BRANCH:-matthew/nice-lovelace-4sqyit}"
export CHAT_DEPLOYMENT="${CHAT_DEPLOYMENT:-gpt-4.1-mini}"
export ADMINS="${ADMINS:-matthews@finishessolutions.com}"
fail() { echo "STOPPED: $*" >&2; exit 1; }

echo "== 1. tools and prerequisites"
command -v az >/dev/null || fail "Azure CLI missing (use Azure Cloud Shell)"
command -v psql >/dev/null || fail "psql missing; run this from Azure Cloud Shell or a Mac with PostgreSQL client tools"
command -v gh >/dev/null || fail "GitHub CLI (gh) missing; see https://cli.github.com"
[ -f local/azure-dev.env ] || fail "local/azure-dev.env not found (the file the ci command uses)"
az cognitiveservices account deployment show -g fs-communication-intelligence -n aoai-fs-ci-dev-1026 \
  --deployment-name "$CHAT_DEPLOYMENT" -o none 2>/dev/null \
  || fail "Azure OpenAI has no deployment named $CHAT_DEPLOYMENT (the console's Lab needs it)"
if ! gh auth status >/dev/null 2>&1; then
  echo "  Sign in to GitHub (a code and a link appear; open the link, paste the code):"
  gh auth login --hostname github.com --git-protocol https --web
fi
echo "  ok"

echo "== 2. admin-console code"
git fetch --quiet origin "$BRANCH" main
git checkout --quiet "$BRANCH" 2>/dev/null || git checkout --quiet -b "$BRANCH" "origin/$BRANCH"
git pull --quiet --ff-only origin "$BRANCH"
echo "  on $BRANCH at $(git rev-parse --short HEAD)"

echo "== 3. Azure resources, sign-in, file storage, database"
bash scripts/azure/4-admin-console.sh

source scripts/azure/common.sh
ADM_HOST="$(app_host "$ADM_APP")"

echo "== 4. GitHub secrets (environment: development)"
gh secret set ADMIN_FUNCTION_APP --repo "$REPO_SLUG" --env development --body "$ADM_APP"
gh secret set ADMIN_PUBLIC_URL --repo "$REPO_SLUG" --env development --body "https://$ADM_HOST"
echo "  ok"

echo "== 5. merge the admin-console pull request (this deploys)"
PR="$(gh pr list --repo "$REPO_SLUG" --head "$BRANCH" --state open --json number -q '.[0].number')"
if [ -n "$PR" ]; then
  gh pr merge "$PR" --repo "$REPO_SLUG" --merge \
    || fail "GitHub refused the merge (checks or reviews pending?). Open https://github.com/$REPO_SLUG/pull/$PR, merge it, then rerun this script."
  echo "  merged #$PR"
else
  echo "  no open pull request for $BRANCH (already merged?); redeploying main"
  gh workflow run deploy-dev.yml --repo "$REPO_SLUG" --ref main
fi

echo "== 6. wait for the deploy"
SHA="$(git ls-remote "https://github.com/$REPO_SLUG.git" refs/heads/main | cut -f1)"
RUN=""
for _ in $(seq 1 30); do
  RUN="$(gh run list --repo "$REPO_SLUG" --workflow deploy-dev.yml --limit 10 \
    --json databaseId,headSha -q ".[] | select(.headSha == \"$SHA\") | .databaseId" | head -1)"
  [ -n "$RUN" ] && break
  sleep 10
done
[ -n "$RUN" ] || fail "no deploy run started for main@${SHA:0:7}; check GitHub -> Actions"
gh run watch "$RUN" --repo "$REPO_SLUG" --exit-status --interval 20 \
  || fail "the deploy failed; see https://github.com/$REPO_SLUG/actions/runs/$RUN"

echo "== 7. check the console"
page="$(curl -s -o /dev/null -w '%{http_code}' "https://$ADM_HOST/")"
api="$(curl -s -o /dev/null -w '%{http_code}' "https://$ADM_HOST/api/overview")"
client="$(curl -s "https://$ADM_HOST/config.json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("client_id") or "")' 2>/dev/null || true)"
echo "  page: HTTP $page (want 200)   api without sign-in: HTTP $api (want 401)   sign-in app: ${client:+configured}"
git checkout --quiet main && git pull --quiet --ff-only origin main
[ "$page" = 200 ] && [ "$api" = 401 ] && [ -n "$client" ] \
  || fail "the console is deployed but not answering as expected; send these three values to Claude"

cat <<OUT

The admin console is live:  https://$ADM_HOST/
Sign in as one of: $ADMINS
This Cloud Shell clone is back on main, with the new ci commands (ci schedule, ci run-capture).
OUT
