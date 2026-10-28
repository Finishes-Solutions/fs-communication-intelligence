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
READERS="matthews@finishessolutions.com chuckc@finishesetc.net" bash scripts/azure/2-search-api-entra.sh
```

This registers the search API (scope `Communications.Search`, app roles `Communications.Reader.All` / `.Own`, assignment required). It gives each address in `READERS` the **Reader.All** role, which means **all enrolled company mail**; use `.Own` in the portal for someone who should only see their own mailbox. It also creates one client app each for Claude, ChatGPT and Copilot Studio, and points the search Function App at the API.

It prints the connector settings and each platform's client ID. It writes **a new client secret per platform** to `~/fs-ci-mcp-client-secrets.txt`, readable only by you, and invalidates the previous secrets. Paste each secret straight into its platform's connector screen, then delete the file. Never paste a secret into chat, email or GitHub; if one leaks, rerun the script to rotate them all. Consent is granted through Microsoft Graph, because `az ad app permission admin-consent` doesn't accept Cloud Shell's sign-in.

## 7. First run: the last 24 hours

The ingestion app checks every 5 minutes whether a capture is due. The schedule lives in the database (table `ops_settings`; change it in the admin console, §12, or with `ci schedule`) and starts **off**. The app settings `CAPTURE_ENABLED` and `PROCESSING_ENABLED` are hard off switches above it. Turn the app switch on:

```bash
az functionapp config appsettings set -g fs-communication-intelligence -n func-fs-ci-ing-dev-1026 --settings CAPTURE_ENABLED=true -o none
```

Ask for one capture of the last 24 hours. It starts at the next 5-minute tick. With nothing approved yet, it only records the mailbox list and refuses to capture:

```bash
ci run-capture --hours 24
```

Wait about 5 minutes, then review every mailbox. Mailboxes outside the Exchange scope show `no_exchange_access`.

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

Capture the 24 hours again, and turn processing on (it runs every 5 minutes, up to 8 minutes each time):

```bash
ci run-capture --hours 24 && az functionapp config appsettings set -g fs-communication-intelligence -n func-fs-ci-ing-dev-1026 --settings PROCESSING_ENABLED=true -o none
```

Watch progress (codes and counts only):

```bash
ci stats
```

Logs: portal → `func-fs-ci-ing-dev-1026` → **Monitoring → Logs** or **Log stream**. Each capture and processing pass is also recorded with its counts (`ci schedule show`, or **Schedule & runs** in the console). Once the cause of a failure is fixed, requeue the failed messages; the process timer picks them up:

```bash
ci process --retry-failed --limit 0
```

If Cloud Shell can't reach the database (timeout), add its address: `az postgres flexible-server firewall-rule create -g fs-communication-intelligence --server-name psql-fs-ci-dev-1026b --name cloudshell --start-ip-address $(curl -s https://api.ipify.org) --end-ip-address $(curl -s https://api.ipify.org)`.

## 8. Test

- `ci stats`: published, held/rejected by reason, failed codes, label counts, attachment extraction.
- Give your own command-line identity read access once: `ci grant add --me`. Agents use the app roles from step 6 instead.
- `mkdir -p local/out && ci actionable --person chuckc@finishesetc.net --hours 24 --out local/out/chuck.md`. This is real content: keep it out of Git and share deliberately.
- Hand-check about 30 action items and 30 random published messages, and note wrong labels. The thresholds are uncalibrated. The console's **Lab** re-runs any message and explains its routing.
- Connect the agents (§10) and ask the same questions.

## 9. Turn on hourly capture

In the console (**Schedule & runs**) or from Cloud Shell:

```bash
ci schedule set --on --every 60 --window 2
```

That captures every hour, looking back 2 hours. The overlap is deliberate, and repeats are skipped. Options: `--every 15|30|60|120|240|720|1440`, `--hours 6-20` (active hours, Central time by default), `--weekdays-only`, `--processing-off`. The look-back must cover the interval, or mail between runs would be missed. New mailboxes (new hires) appear as `enrolled` but are **not** captured until approved; run `ci scope include --all-enrolled` (or **Approve all enrolled** in the console) when you add people.

Stop scheduled capture: `ci schedule set --off`. Stop everything at once (search also returns nothing while paused):

```bash
ci mode set paused
```

Hard stop for the timers, whatever the schedule says:

```bash
az functionapp config appsettings set -g fs-communication-intelligence -n func-fs-ci-ing-dev-1026 --settings CAPTURE_ENABLED=false PROCESSING_ENABLED=false -o none
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

## 12. Admin console

A web page at `https://func-fs-ci-adm-dev-1026.azurewebsites.net/` for console admins (you, for now):

- **Overview:** mode, schedule, backlog, message states and reasons, labels, attachment extraction, failures, and recent runs. Also **Run capture now** (any look-back) and **Requeue failed**.
- **Schedule & runs:** how often capture runs, its look-back, active hours, weekdays only, processing on/off. Below that, every run with its counts.
- **Mailboxes:** filter, approve or unapprove, exclude or un-exclude (with a reason), approve all enrolled, change mode, and exclude a person or domain in every mailbox.
- **Messages:** search or list published mail by mailbox, sender, label and date. A message shows the AI summary, action and risk, labels, people, the body, and its **files**: preview (PDF, images, text), download, or the extracted text. Signature images and logos aren't processed or shown: every inline image, and any attached image of 100 KB or less. There's also an **Open in Outlook** link, which works only for mailboxes your account can open.
- **Action items:** the same list agents get from `list_actionable`, for any person or window.
- **Not published:** held, restricted, bulk and failed messages, with metadata and reason only, never the subject or text.
- **Communication Lab:** write an email (or start from a sample: invoice, change order, lien notice, failed inspection, newsletter, phishing, SSN, prompt injection), or pick a stored one. It runs the real screening, classifier, policy and routing and stores nothing. It shows a verdict ("Action item", "Project risk", "Restricted"…), each pipeline step, why, the AI proposal, every label against its thresholds, the difference from what was stored, and the message's files. Each run calls Azure OpenAI once.

**Original files.** From the deploy that includes this, processing keeps each attachment's original file in the private container `attachments` of `stfscievidev1026`. Each distinct file is stored once, and inline signature images are skipped. Each processing pass also backfills up to 100 files for mail processed earlier. Files are **not malware-scanned** (your decision, 2026-10-28), so open unexpected downloads with care. The console serves a file only when you may read its message; restricted, held and bulk mail never get file access.

Access takes both the delegated scope `Communications.Admin` and the app role `Communications.Admin` on the search API registration. Everyone else gets 403. Content goes through the same authorized search as the agents, so the console shows exactly what your Reader role allows. Every change and every lab run is written to `audit_events` (lab runs record the input hash only). The app has its own identity (`id-fs-ci-admin-dev`), with database roles `ci_operator` and `ci_search` and no mailbox access.

**One command does everything**: Azure resources, sign-in, file storage, database changes, GitHub secrets, merging the pull request, waiting for the deploy and checking the result. Run it from Cloud Shell in the repository folder (it asks you to sign in to GitHub once):

```bash
cd ~/fs-communication-intelligence && git fetch origin && git checkout matthew/nice-lovelace-4sqyit && bash scripts/azure/5-admin-console-live.sh
```

The console is the web page and its API in one Function App (`func-fs-ci-adm-dev-1026`): same address, no separate Azure Static Web Apps resource, and no cross-site setup.

The steps it runs, if you'd rather go one at a time. First see what already exists (this changes nothing):

```bash
CHECK_ONLY=1 bash scripts/azure/4-admin-console.sh
```

Then run it. It creates only what's missing, sets up file storage, applies the database migrations and creates the console's database login:

```bash
CHAT_DEPLOYMENT=gpt-4.1-mini ADMINS="matthews@finishessolutions.com" bash scripts/azure/4-admin-console.sh
```

Add the two secrets it prints (`ADMIN_FUNCTION_APP`, `ADMIN_PUBLIC_URL`) to the GitHub environment `development`, then merge the pull request (or rerun **Deploy (development)**). The migrations (`0005`, `0006`) must be applied **before** the new ingestion code deploys; otherwise the capture and processing timers fail every tick until they are. To add another admin later, rerun the script with their address in `ADMINS`.

## What isn't done yet

- Per-folder delta sync and new-mail notifications (the hourly window is the stopgap); deletions and moves after capture aren't tracked.
- REST endpoints (MCP only for now), dead-letter alerts and dashboards, and infrastructure as Bicep (these scripts are the interim).
- monday.com Executive Hub feed.
- OCR and malware scanning for attachments (the extraction and original-file exceptions to L11 still apply).
