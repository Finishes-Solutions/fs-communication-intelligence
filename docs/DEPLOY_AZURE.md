# Deploy to Azure (dev): Function Apps, GitHub Actions, hosted MCP

This replaces the laptop-certificate route in [RUN_24H_TEST.md](RUN_24H_TEST.md). Mail is read by the **ingestion managed identity** (`id-fs-ci-ingestion-dev`) inside Azure, and the MCP endpoint is hosted, so Claude, ChatGPT and Copilot Studio can reach it with each person's own Microsoft sign-in.

```
Graph (Exchange RBAC) -> func-fs-ci-ing-dev-1026  --(ci_ingest)-->  PostgreSQL psql-fs-ci-dev-1026b
   capture (hourly)        process (every 5 min)                         |
                                                               (ci_search, read-only views)
Claude / ChatGPT / Copilot Studio --Entra sign-in--> func-fs-ci-srch-dev-1026 /mcp
```

Status: every piece is tested against the synthetic tenant, including the hosted endpoint, the Azure Functions adapter, and least-privilege identities on a simulated Azure database. **Nothing has run in Azure, Entra, Exchange or the agent platforms yet.** "Check" lines tell you what success looks like at each step; if one fails, send me the error text (never secrets or email content).

Work in **Azure Cloud Shell → Bash** (portal.azure.com, `>_`). It already has `az`, `git`, `make`, `psql` and `python3`, and it's inside Azure, so it can reach the database once step 3 opens it to Azure services.

Select the subscription that holds the `fs-communication-intelligence` resource group (every script uses the current one):

```bash
az account set --subscription "Finishes 3.0 Azure Subscription" && az group show -n fs-communication-intelligence --query name -o tsv
```

```bash
git clone https://github.com/Finishes-Solutions/fs-communication-intelligence.git && cd fs-communication-intelligence && git checkout matthew/nice-lovelace-4sqyit
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.bashrc && uv sync
```

## 1. Mail access for the ingestion identity (start first: up to ~2 hours to apply)

```bash
bash scripts/azure/3-mail-access.sh
```

Check: `Graph User.Read.All -> id-fs-ci-ingestion-dev: ok`, then it prints Exchange PowerShell commands with the IDs filled in. Switch Cloud Shell to **PowerShell** and run them, after tagging any mailbox that must stay out with `CustomAttribute15 = FS-CI-Exclude`. Check: `Test-ServicePrincipalAuthorization` shows `InScope : True`.

If you created the laptop-test app ("FS CI Email Ingestion Test") from RUN_24H_TEST.md, you don't need it now. Delete it and its role assignment (RUN_24H_TEST.md §7).

## 2. Model deployments

In `aoai-fs-ci-dev-1026` → **Go to Azure AI Foundry portal** → **Deployments**: deploy `text-embedding-3-large` (name it exactly that) and a chat model with structured outputs, such as `gpt-4.1-mini`. Note the chat deployment name.

## 3. Function Apps, Azure roles and GitHub connection

```bash
CHAT_DEPLOYMENT=gpt-4.1-mini bash scripts/azure/1-function-apps.sh
```

This creates `func-fs-ci-ing-dev-1026` and `func-fs-ci-srch-dev-1026` (Flex Consumption, Python 3.14) on the existing identities and storage accounts. It sets their app settings with **capture and processing off**, grants each identity only its AI-service and storage roles, adds a PostgreSQL firewall rule for Azure services, and connects GitHub (federated credential + deploy role). It ends by printing six values for GitHub.

If `az functionapp create` rejects a flag or Python 3.14, send me the message. Flex Consumption options change; the fix is usually a one-line edit.

**Security note.** The "Azure services" firewall rule accepts connections from any Azure-hosted source, though each still needs a valid Entra login to this server. It's acceptable for dev. Production uses private networking (ADR-011).

## 4. GitHub: environment and secrets

GitHub → repository → **Settings → Environments → New environment** `development`:
- **Deployment branches:** *Selected branches* → `main`.
- **Environment secrets:** the six values printed in step 3: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `INGESTION_FUNCTION_APP`, `SEARCH_FUNCTION_APP`, `SEARCH_PUBLIC_URL`. They're IDs, not passwords. They're stored as secrets so this public repo's logs mask them.

The deploy workflow (`.github/workflows/deploy-dev.yml`) runs when this branch is merged to `main`, and later from **Actions → Deploy (development) → Run workflow**. GitHub only offers "Run workflow" once the file is on `main`, so the first deployment happens at the merge. Check: the run ends with `OAuth metadata: ok`.

## 5. Database

Create `local/azure-dev.env` (Cloud Shell):

```bash
cat > local/azure-dev.env <<EOF
DEPLOYMENT_ENVIRONMENT=development
DATABASE_AUTH=entra
PGHOST=psql-fs-ci-dev-1026b.postgres.database.azure.com
PGPORT=5432
PGUSER=$(az ad signed-in-user show --query userPrincipalName -o tsv)
PGDATABASE=ci
PGSSLMODE=require
GRAPH_TENANT_ID=$(az account show --query tenantId -o tsv)
AUTH_MODE=entra
EOF
```

Make yourself the server's Entra admin and allow the extensions (both are one-time; the portal equivalents are **Security → Authentication** and **Server parameters → azure.extensions**):

```bash
az postgres flexible-server microsoft-entra-admin create -g fs-communication-intelligence -s psql-fs-ci-dev-1026b --display-name "$(az ad signed-in-user show --query userPrincipalName -o tsv)" --object-id "$(az ad signed-in-user show --query id -o tsv)"
```

```bash
az postgres flexible-server parameter set -g fs-communication-intelligence -s psql-fs-ci-dev-1026b --name azure.extensions --value VECTOR,PGCRYPTO
```

Apply the schema, then create the two identities' database logins:

```bash
make azure-migrate AZ_PSQL=psql && make azure-db-principals AZ_PSQL=psql
```

If Cloud Shell has no `psql`, run these two `make` commands from your Mac instead (Docker running, same `local/azure-dev.env`, without `AZ_PSQL=psql`). Check: `pgvector 0.8.x`, then a table listing `id-fs-ci-ingestion-dev | ci_ingest` and `id-fs-ci-search-dev | ci_search`. Register the tenant:

```bash
alias ci='uv run --env-file local/azure-dev.env ci' && ci tenant init
```

## 6. Search API sign-in (Entra) and who may read

```bash
READERS="matthews@finishessolutions.com chuck@finishessolutions.com" bash scripts/azure/2-search-api-entra.sh
```

This registers the search API (scope `Communications.Search`, app roles `Communications.Reader.All` / `.Own`, assignment required). It gives each address in `READERS` the **Reader.All** role, which means **all enrolled company mail**; use `.Own` in the portal for someone who should only see their own mailbox. It also creates one client app each for Claude, ChatGPT and Copilot Studio, and points the search Function App at the API.

It prints connector settings and **one client secret per platform, once**. Paste each secret straight into that platform's connector screen. Never paste it into chat, email or GitHub. Rerun the script to rotate secrets.

## 7. First run: the last 24 hours

The ingestion app is deployed with `CAPTURE_HOURS=24` and a capture schedule that only fires on 1 January, so nothing runs by itself. Set up helpers:

```bash
ING=func-fs-ci-ing-dev-1026; RG=fs-communication-intelligence; HOST=$(az functionapp show -g $RG -n $ING --query defaultHostName -o tsv); KEY=$(az functionapp keys list -g $RG -n $ING --query masterKey -o tsv)
run_capture() { curl -s -o /dev/null -w "capture triggered: HTTP %{http_code}\n" -X POST "https://$HOST/admin/functions/capture" -H "x-functions-key: $KEY" -H "Content-Type: application/json" -d '{}'; }
```

Turn capture on and trigger it once. With nothing approved yet, it only records the mailbox list and refuses to capture. Check: `HTTP 202`.

```bash
az functionapp config appsettings set -g $RG -n $ING --settings CAPTURE_ENABLED=true -o none && sleep 60 && run_capture
```

Wait a minute, then review every mailbox. Mailboxes outside the Exchange scope show `no_exchange_access`.

```bash
ci scope list
```

Keep anyone out, then approve the rest:

```bash
ci scope exclude someone@finishessolutions.com --reason personal_mailbox
```

```bash
ci scope include --all-enrolled && ci mode show
```

Capture the 24 hours and start processing (every 5 minutes, about 8 minutes per run):

```bash
run_capture && az functionapp config appsettings set -g $RG -n $ING --settings PROCESSING_ENABLED=true -o none
```

Watch progress (codes and counts only):

```bash
ci stats
```

Logs: portal → `func-fs-ci-ing-dev-1026` → **Monitoring → Logs** or **Log stream**. Once the cause of a failure is fixed, requeue the failed messages (the process timer picks them up):

```bash
ci process --retry-failed --limit 0
```

If Cloud Shell can't reach the database (timeout), add its address: `az postgres flexible-server firewall-rule create -g $RG -n psql-fs-ci-dev-1026b --rule-name cloudshell --start-ip-address $(curl -s https://api.ipify.org) --end-ip-address $(curl -s https://api.ipify.org)`.

## 8. Test

- `ci stats`: published, held/rejected by reason, failed codes, label counts, attachment extraction.
- Give your own command-line identity read access once: `ci grant add --me`. Agents use the app roles from step 6 instead.
- `mkdir -p local/out && ci actionable --person chuck@finishessolutions.com --hours 24 --out local/out/chuck.md`. This is real content: keep it out of Git and share deliberately.
- Hand-check about 30 action items and 30 random published messages, and note wrong labels. The thresholds are uncalibrated.
- Connect the agents (§10) and ask the same questions.

## 9. Turn on hourly capture

```bash
az functionapp config appsettings set -g $RG -n $ING --settings CAPTURE_HOURS=2 "CAPTURE_SCHEDULE=0 5 * * * *" -o none
```

That captures at 5 past every hour. The 2-hour window overlaps the previous run, and repeats are skipped. New mailboxes (new hires) appear as `enrolled` but are **not** captured until approved; run `ci scope include --all-enrolled` when you add people.

Stop everything at once (search also returns nothing while paused):

```bash
ci mode set paused
```

Stop only the timers:

```bash
az functionapp config appsettings set -g $RG -n $ING --settings CAPTURE_ENABLED=false PROCESSING_ENABLED=false -o none
```

## 10. Connect Claude, ChatGPT and Copilot Studio

All three use the values printed in step 6: **MCP server URL** `https://<search host>/mcp`, the Entra **authorization** and **token** URLs, scope `api://<API id>/Communications.Search offline_access`, and that platform's own **client ID and secret**. Each person signs in as themselves. The server then shows them only what their app role and grants allow, and every call is audited.

| Platform | Where | Notes |
|---|---|---|
| **Claude** (Team/Enterprise) | Admin settings → Connectors → Add custom connector: URL, then **Advanced settings** → OAuth client ID and secret | Redirect URI registered: `https://claude.ai/api/mcp/auth_callback`. If Claude shows a different callback, add it to the "FS Communications MCP - Claude" app registration. |
| **Copilot Studio** | Agent → **Tools → Add a tool → Model Context Protocol** → OAuth 2.0, *Manual*: authorization URL, token URL, refresh URL (same as token), client ID/secret, scope | After saving, Copilot shows a **redirect URL**; add it to "FS Communications MCP - Copilot Studio" (Entra → App registrations → Authentication). Set the connection to **end-user credentials**, otherwise everyone using the agent searches as the maker. |
| **ChatGPT** (Business/Enterprise, workspace admin enables custom connectors) | Settings → Connectors (Apps) → Create: URL, OAuth | **Highest risk.** ChatGPT has relied on OAuth *dynamic client registration*, which Entra doesn't offer. If it won't accept the pre-registered client ID/secret, ChatGPT is blocked. The fix is a small OAuth broker in front of Entra (CLAUDE.md §8). I'll build that only once you confirm it's needed. |

The hosted endpoint is written to the MCP standard (Streamable HTTP; OAuth protected-resource metadata at `/.well-known/oauth-protected-resource/mcp`). One known Entra quirk to watch for: some clients send a `resource` parameter that Entra rejects with **AADSTS9010010**. If you see that code, send it to me.

## 11. Copilot Studio with Azure AI Search or other tools

- **Don't give Copilot Studio a search index or database of the mail.** Azure AI Search isn't part of this system: PostgreSQL is the search index (L2), and there's no AI Search resource. A direct index or database connection would skip the per-person authorization, exclusions and audit; every agent must go through this MCP endpoint (L8).
- **Other tools: yes.** These include monday.com, SharePoint, Outlook calendar, Teams, Word/Excel output, and Power Automate flows to post a daily digest. Each acts with the signed-in user's own permissions in that system. The agent can combine them, for example: find actions via MCP, check the monday board, draft a report.
- If you later want Azure AI Search (for semantic ranking at higher volume), it goes behind this same API (ADR-012's `SearchBackend`), never handed to agents directly.

## What isn't done yet

- Per-folder delta sync and new-mail notifications (the hourly window is the stopgap); deletions and moves after capture aren't tracked.
- REST endpoints (MCP only for now), dead-letter alerts and dashboards, and infrastructure as Bicep (these scripts are the interim).
- monday.com Executive Hub feed.
- OCR and malware scanning for attachments (the extraction exception to L11 still applies).
