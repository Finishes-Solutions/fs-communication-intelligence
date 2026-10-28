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

Logs: portal → `func-fs-ci-ing-dev-1026` → **Monitoring → Logs** or **Log stream**. Each capture and processing pass is also recorded with its counts (`ci schedule show`, or **Schedule & Runs** in the console). Once the cause of a failure is fixed, requeue the failed messages; the process timer picks them up:

```bash
ci process --retry-failed --limit 0
```

Attachments that failed to read (for example before the 2026-10-28 fix that let the reader find its PDF and Excel libraries in Azure) can be read again. This reprocesses their messages with the attachment text included; each one is unsearchable for a few minutes while that happens:

```bash
ci process --retry-attachments --limit 0
```

**Masking (2026-10-28).** Bank, routing, card and government ID numbers and credentials (passwords, one-time codes, API keys, tokens, reset links) are now masked, e.g. `[USBankAccountNumber]` or `[credential]`, before an email is classified, indexed or shown. The email itself stays searchable, flagged for review. Health information is still hidden entirely. Mail processed before this change still has the old outcome (hidden, or indexed unmasked). Once the deploy with masking has finished, reprocess everything under the new rules, then turn processing back on if you paused it:

```bash
ci process --remask --limit 0
```

```bash
ci schedule set --processing-on
```

Each message is unsearchable for a few minutes while it is reprocessed. `SENSITIVE_DATA_POLICY=restrict` on the ingestion app brings back the old rule. The original attachment files kept in Blob Storage are not masked; only the admin console can open them.

**Cost controls (2026-10-28).** Two changes cut the per-email cost of the AI services:

- *Bulk and automated mail is skipped before any paid step.* A message from a no-reply, notifications or alerts address, or with an unsubscribe link (and not a reply or forward), is recorded as not indexed with reason `not_indexed:automated_sender` or `not_indexed:bulk_unsubscribe`. Prompt Shields, masking, classification and embeddings are not called. Mail with a real attachment (not a signature image or logo) is never skipped, because invoices and scans often come from no-reply addresses. Some systems send project content from a no-reply address, e.g. a project-management tool's RFI or change-order notices. List their domains on the ingestion app so they are always processed in full: `BULK_PREFILTER_ALLOW_DOMAINS=example-pm.com,example-sign.net`. `BULK_PREFILTER_ENABLED=false` turns the rule off. `ci stats` lists the skipped mail by sender domain. To process skipped mail in full afterwards (all of it, or one domain), run this; the paid steps then run for those messages:

```bash
ci process --retry-skipped --domain example-pm.com --limit 0
```

- *Prompt Shields reads only what the classifier reads*: the subject, the first 12,000 characters of the body, and the first 3,000 characters of up to 5 attachments. Masking still covers all indexed text. The trade-off: text past those limits is indexed without the prompt-injection check. Agents are already told that returned email text is untrusted. With `SENSITIVE_DATA_POLICY=restrict` the whole text is still screened, because that mode's PII check must see everything indexed.

**Testing switches for the paid checks (2026-10-28).** In the admin console, Schedule & Runs has a *Paid Safety Checks* card with two switches. The same switches are on the command line: `ci schedule set --shield-off --masking-off` (`--shield-on --masking-on` to undo).

- *Prompt-injection check* (Prompt Shields) off: no injection check runs. The classifier still ignores instructions in email text.
- *Masking* (Azure AI Language) off: nothing is masked. Mail that the classifier marks as holding ID, bank or card numbers or credentials is **restricted** (hidden) instead of masked. That label is uncalibrated and catches less than the Language service.

Every message processed with a switch off records it. To redo those messages with the checks on later (the paid steps run again), use:

```bash
ci process --rescreen --limit 0
```

The switches apply only under `SENSITIVE_DATA_POLICY=mask` and are for testing, not production.

If Cloud Shell or your computer can't reach the database (timeout), add its address with `bash scripts/azure/allow-my-ip.sh <name>` (e.g. `matthew-work`); `--list` shows who is allowed and `<name> --remove` takes one away. The script copes with Azure CLI versions that name the firewall arguments differently (`--server-name/--name` or `--name/--rule-name`).

### Backfill further back

A requested capture can reach back up to 93 days. Give a start date (midnight Central time) instead of hours:

```bash
ci run-capture --since 2026-09-01
```

The ingestion app stops a single run after 2 hours. If a large backfill is cut off, request it again: it resumes, because emails already stored are skipped. Keep processing paused (`ci schedule set --processing-off`) while capturing if you want to change settings before the AI runs.

## 8. Test

- `ci stats`: published, held/rejected by reason, failed codes, label counts, attachment extraction.
- Give your own command-line identity read access once: `ci grant add --me`. Agents use the app roles from step 6 instead.
- `mkdir -p local/out && ci actionable --person chuckc@finishesetc.net --hours 24 --out local/out/chuck.md`. This is real content: keep it out of Git and share deliberately.
- Hand-check about 30 action items and 30 random published messages, and note wrong labels. The thresholds are uncalibrated. The console's **Lab** re-runs any message and explains its routing.
- Connect the agents (§10) and ask the same questions.

## 9. Turn on hourly capture

In the console (**Schedule & Runs**) or from Cloud Shell:

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

### Search speed

Search is hybrid: meaning (vector similarity on the embeddings) plus keywords (PostgreSQL full text), fused with Reciprocal Rank Fusion. Migration 0015 lets the meaning half use the vector index. Before it, every search compared the question with every indexed chunk, so searches slowed as mail was added. Apply it like the others (`python3 db/migrate.py apply`). Until it is applied, search keeps working the old way.

Check speed and accuracy on your own mail. This prints numbers only:

```bash
ci search-check --samples 20
```

`recall_at_10` is the share of the exact 10 nearest chunks the index also found. Near 1.0 is good. Below about 0.9, tell Claude: the index can be tuned. `index_ms` against `exact_ms` shows the speed-up.

Each search logs its timings to App Insights, with no query text: `search timings ms: embed=… query=… metadata=… coverage=… total=… path=index|exact|exact_fallback`. In the portal, open `appi-fs-ci-dev` → Logs and run:

```kusto
traces | where message startswith "search timings" | order by timestamp desc | take 50
```

Other things that make a search slow:
- **The first search after a quiet period** starts the search app (a cold start, several seconds).
- **Processing running at full speed** competes with search for the embedding quota (a search waits at most 20 seconds) and for the database's CPU.
- **The database tier:** `B2ms` is a burstable tier. Under long processing runs it can use up its CPU credits and slow down, and `B2s` would be slower. Keep `B2ms` or larger while mail is being processed and searched.

### Processing speed and Azure OpenAI rate limits

`PROCESSING_WORKERS` (app setting) is how many emails are processed at once, and `PROCESSING_MINUTES` is how long each 5-minute timer run may work. The embedding deployment's tokens-per-minute quota sets the real ceiling: Azure checks it over windows of about 10 seconds, so a few emails with long attachments sent at once get HTTP 429 even when the minute's total would fit.

All workers share one embedding budget, `EMBEDDING_TOKENS_PER_MINUTE` (default 300,000, below the dev deployment's 350K TPM so search queries still get through). After any 429, every worker waits for the service's Retry-After. An email fails only if throttling lasts more than 5 minutes for one request. Search queries wait at most 20 seconds. If you raise the deployment's capacity in the Azure portal, raise the setting to about 85% of it:

```bash
az functionapp config appsettings set -g fs-communication-intelligence -n func-fs-ci-ing-dev-1026 --settings EMBEDDING_TOKENS_PER_MINUTE=300000 -o none
```

More workers than about 16 do not help once embeddings are the limit. Mail that failed before this change: **Requeue failed** in Schedule & Runs (or `ci process --retry-failed`). A retried email is classified again, so the retry costs one more chat call.

## 10. Connect Claude, ChatGPT and Copilot Studio

All three use the values printed in step 6: **MCP server URL** `https://<search host>/mcp`, the Entra **authorization** and **token** URLs, scope `api://<API id>/Communications.Search offline_access`, and that platform's own **client ID and secret**. Each person signs in as themselves. The server then shows them only what their app role and grants allow, and every call is audited.

| Platform | Where | Notes |
|---|---|---|
| **Claude** (Team/Enterprise) | Admin settings → Connectors → Add custom connector: URL, then **Advanced settings** → OAuth client ID and secret | Redirect URI registered: `https://claude.ai/api/mcp/auth_callback`. If Claude shows a different callback, add it to the "FS Communications MCP - Claude" app registration. |
| **Copilot Studio** | Agent → **Tools → Add a tool → Model Context Protocol** → OAuth 2.0, *Manual*: authorization URL, token URL, refresh URL (same as token), client ID/secret, scope | After saving, Copilot shows a **redirect URL**; add it to "FS Communications MCP - Copilot Studio" (Entra → App registrations → Authentication). Set the connection to **end-user credentials**, otherwise everyone using the agent searches as the maker. |
| **ChatGPT** (developer mode for custom plugins; in a Business/Enterprise workspace an admin may need to allow it) | Plugins → **Add** → Add custom MCP server: Server URL, Authentication **OAuth** → **Advanced OAuth settings**: registration method **User-Defined OAuth Client**, client ID/secret, token endpoint auth method **client_secret_post**, base scopes `api://<API id>/Communications.Search offline_access`, Auth URL and Token URL (discovered), Registration URL empty, **Resource = `api://<API id>`** (not the server URL), PKCE ticked, **OIDC unticked** | **Verified 2026-10-28.** Each plugin gets its own callback URL (`https://chatgpt.com/connector/oauth/<id>`), shown in the dialog and changed if the plugin is recreated; add it to "FS Communications MCP - ChatGPT" (Entra → App registrations → Authentication, or `az ad app update --web-redirect-uris`, keeping the existing ones) before signing in, or sign-in fails with AADSTS50011. With Resource left as the server URL, sign-in succeeds but ChatGPT never gets a token (Entra refuses the mismatched resource) and shows "We couldn't connect your account". With OIDC on, ChatGPT asks Graph for the profile with a token meant for this API. No OAuth broker is needed. |

The hosted endpoint is written to the MCP standard (Streamable HTTP; OAuth protected-resource metadata at `/.well-known/oauth-protected-resource/mcp`). One known Entra quirk to watch for: some clients send a `resource` parameter that Entra rejects with **AADSTS9010010**. If you see that code, send it to me.

**What the MCP tools do.** Every tool shows only what the signed-in person may read, and every call is audited (IDs and counts, never text).

| Tool | Use it for |
|---|---|
| `search_email` | Finding emails by meaning and keywords, with filters for mailbox, sender, label, property, entity and dates |
| `list_email` | The newest emails matching filters, e.g. a mailbox's mail since a date |
| `list_actionable` | Action items, approvals and project risks; with `person`, that person's (the per-person check tells Chuck Sr. from Chuck Jr.) |
| `get_email` | One email in full, with its people, labels and attachments |
| `get_attachment` | An attachment's extracted text, a page at a time |
| `get_thread` | The whole conversation an email belongs to, oldest first |
| `email_stats` | Counts of emails, action items and risks by mailbox, sender, domain, label, property, entity, day, week or month |
| `risk_overview` | Project-risk trends: change from the previous period, top categories, properties, companies and mailboxes, newest risks |

Dates: `since_date` and `until_date` (YYYY-MM-DD, both inclusive) pick an exact period on `search_email`, `list_email` and `email_stats`.

**Links and attachments in results.** Outlook links (`web_link`) open only in the mailbox owner's own Outlook. Every email in MCP results therefore also carries `view_url`, a read-only page on the admin console (`/read/<id>`). It opens for **console admins only**, after Microsoft sign-in; anyone else sees "Can't open this email". Stored attachments carry `download_url` (the same page with `?file=`, which downloads the original). Attachment files are not malware-scanned yet, so keep downloads to admins. The tool `get_attachment` returns an attachment's full extracted text a page at a time (pass the `attachment_id` that `get_email` lists); the file itself never goes to Copilot, ChatGPT or Claude. The deploy workflow sets the search app's `ADMIN_PUBLIC_URL` from the GitHub secret of the same name; without it, results carry no `view_url`.

**Troubleshooting a connection.** The search app logs every request (method, path, status, whether a token was sent) and why a token was rejected, never the token. Check after a connection attempt:

```bash
az monitor app-insights query -g fs-communication-intelligence --app appi-fs-ci-dev --analytics-query "traces | where timestamp > ago(1h) and (message has 'token rejected' or message has 'mcp request') | project timestamp, message | order by timestamp desc | take 30" --query "tables[0].rows" -o tsv
```

(`-o table` prints nothing for this command; keep `--query "tables[0].rows" -o tsv`.) Only `bearer token: no` lines after you signed in means the client never got a token: look in Entra's sign-in logs, tab **User sign-ins (non-interactive)**. `token rejected: <reason>` means the server refused the token for that reason. `bearer token: yes` with 200 means it works.

## 11. Copilot Studio with Azure AI Search or other tools

- **Don't give Copilot Studio a search index or database of the mail.** Azure AI Search isn't part of this system: PostgreSQL is the search index (L2), and there's no AI Search resource. A direct index or database connection would skip the per-person authorization, exclusions and audit; every agent must go through this MCP endpoint (L8).
- **Other tools: yes.** These include monday.com, SharePoint, Outlook calendar, Teams, Word/Excel output, and Power Automate flows to post a daily digest. Each acts with the signed-in user's own permissions in that system. The agent can combine them, for example: find actions via MCP, check the monday board, draft a report.
- If you later want Azure AI Search (for semantic ranking at higher volume), it goes behind this same API (ADR-012's `SearchBackend`), never handed to agents directly.

## 12. Admin console

A web page at `https://func-fs-ci-adm-dev-1026.azurewebsites.net/` for console admins (you, for now):

- **Overview:** mode, schedule, backlog, message states and reasons, labels, attachment extraction, failures, and recent runs. Also **Run Capture Now** (any look-back) and **Requeue Failed**.
- **Schedule & Runs:** how often capture runs, its look-back, active hours, weekdays only, processing on/off. Below that, every run with its counts.
- **Mailboxes:** filter, approve or unapprove, exclude or un-exclude (with a reason), approve all enrolled, change mode, and exclude a person or domain in every mailbox.
- **Messages:** search or list published mail by mailbox, sender, label and date. A message shows the AI summary, action and risk, labels, people, the body, and its **files**: preview (PDF, images, text), download, or the extracted text. Signature images and logos aren't processed or shown: every inline image, and any attached image of 100 KB or less. There's also an **Open in Outlook** link, which works only for mailboxes your account can open.
- **Action Items:** the same list agents get from `list_actionable`, for any person or window. With a person profile (below), it shows that person's confident items plus a separate **Possible** list, and has a panel to edit profiles.
- **Not Published:** held, restricted, bulk and failed messages, with metadata and reason only, never the subject or text.
- **Labels:** shown by name ("Payment", "Needs Approval") or by stored key (`cat_payment_billing`, `approval_required`); switch with **Labels: Names / Keys** in the sidebar. Hovering a label shows the other form. The choice is remembered in that browser.
- **Risk Analysis:** emails labelled Project Risk in the last 7, 30, 90 or 365 days: count and change against the period before, a chart per day or week, counts by category, property/project, sender company (email domain) and mailbox, how many are also urgent or need approval, and a feed with the AI's one-line risk and the full message. It also shows the risk label's measured precision and recall from Label Review. There is no risk type or severity yet, so nothing is grouped by those.
- **Reports:** pick a dataset (messages, action items, project risks, run history, mailboxes, or label review results), a date range, filters and columns, and optionally group by mailbox, sender, sender company, label, property/project, legal entity, day, week or month. Preview on screen, then export to CSV or Excel (up to 5,000 rows). The action-items dataset is exactly the Action Items list (including the person check when the person has a profile). Templates cover common reports; **Save Report** keeps a report in this browser with its date range relative ("last 30 days"). Reports hold metadata and AI proposals only (subject, sender, mailbox, date, labels, properties, entities, summary, action, risk). **Email bodies and attachment text are never exported.** Every preview and export is written to `audit_events` with the message ids it included. Cells that a spreadsheet could run as a formula are neutralized (subjects are untrusted).
- **Label Review** (formerly Label check): go through emails one at a time (from a person's action items, recent mail, project risks, or a label) and mark each label *Yes* / *No* next to what the AI said, and whether it's really an action item for the person, with a reason when it isn't (someone else's, FYI only, automated, group email, already handled). **Results** shows how often the AI is right per label (precision and recall) and, once a label has 20 checked emails with at least 5 of each answer, the "yes" threshold that would have scored best, plus how often listed action items were really each person's. Suggestions are never applied automatically. A message's **Review Labels** button opens it for review.
- **Communication Lab:** write an email (or start from a sample: invoice, change order, lien notice, failed inspection, newsletter, phishing, SSN, prompt injection), or pick a stored one. It runs the real screening, classifier, policy and routing and stores nothing. It shows a verdict ("Action item", "Project risk", "Restricted"…), each pipeline step, why, the AI proposal, every label against its thresholds, the difference from what was stored, and the message's files. Each run calls Azure OpenAI once.

**Original files.** From the deploy that includes this, processing keeps each attachment's original file in the private container `attachments` of `stfscievidev1026`. Each distinct file is stored once, and inline signature images are skipped. Each processing pass also backfills up to 100 files for mail processed earlier. Files are **not malware-scanned** (your decision, 2026-10-28), so open unexpected downloads with care. The console serves a file only when you may read its message; restricted, held and bulk mail never get file access.

Access takes both the delegated scope `Communications.Console` and the app role `Communications.Admin` on the search API registration. Everyone else gets 403. Content goes through the same authorized search as the agents, so the console shows exactly what your Reader role allows. Every change and every lab run is written to `audit_events` (lab runs record the input hash only). The app has its own identity (`id-fs-ci-admin-dev`), with database roles `ci_operator` and `ci_search` and no mailbox access.

**One command does everything**: Azure resources, sign-in, file storage, database changes, GitHub secrets, merging the pull request, waiting for the deploy and checking the result. Run it from Cloud Shell in the repository folder (it asks you to sign in to GitHub once):

```bash
cd ~/fs-communication-intelligence && git fetch origin && git checkout matthew/nice-lovelace-4sqyit && git pull --ff-only origin matthew/nice-lovelace-4sqyit && bash scripts/azure/5-admin-console-live.sh
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

### Costs page

**Costs** in the console shows two figures side by side:

- **Billed:** what Azure charges for the resource group, by service and day. Azure updates it every 8–24 hours.
- **Estimated:** counted as each AI call runs. It covers OpenAI tokens and text records for masking and the prompt-injection check, priced with unit prices you can edit on the page. It is shown per processing run and per 1,000 emails.

The default prices are unverified list prices. Compare the two figures after a day of processing and correct the prices.

To set it up once:

1. Apply database migration 0013 (`db/migrate.py apply`, as for the other migrations).
2. Connect Azure's billed figures. This gives the console's identity the read-only Cost Management Reader role on this resource group only, and tells the console which subscription to read:

   ```bash
   bash scripts/azure/6-cost-dashboard.sh
   ```

A new role can take up to 30 minutes to apply. Until then the page says so.

### Choosing the AI model

The **AI Model** card on the Costs page lists the chat deployments on the Azure OpenAI account and lets you pick the one that classifies email and runs the person check. For each it shows a price, the classification cost per 1,000 emails, and this month's usage.

- **Use This Model** first classifies a synthetic test email; a model that fails is not switched to.
- New mail uses the new model from the next processing run. Mail already processed keeps its labels.
- **Use App Setting** goes back to `AZURE_OPENAI_CHAT_DEPLOYMENT`.
- New models are deployed in the Azure AI Foundry portal first. The embedding model stays text-embedding-3-large.

The list needs migration 0014 and `scripts/azure/6-cost-dashboard.sh`. That script also gives the console read-only access to the Azure OpenAI account. Until then, you can type a deployment name instead.

### Who has access

Run these in Cloud Shell or on a Mac signed in to Azure:

```bash
bash scripts/azure/people.sh list
```

```bash
bash scripts/azure/people.sh add someone@finishessolutions.com
```

`add` grants both the admin console and search from Claude, ChatGPT and Copilot; add `console` or `search` after the address for just one. `remove` takes the same arguments. Both kinds of access show everyone's captured mail, so keep the list short.

### Action items in two steps, and person profiles

1. **Step 1, the classifier,** tags whether an email asks *someone* to act (`action_required`, `approval_required`). It is told to say no for FYI, newsletters, marketing, automated notifications and requests to a whole group.
2. **Step 2, the person check,** runs for each person with a profile. A separate AI call decides whether the email is really *that person's* action item. It sees the profile (name, addresses, role, and who they are **not**), who the email went to directly and who was only copied. It returns a probability, the person's own action line and a reason. Rules answer first without the AI: no request, possible bulk mail, automated sender, sent by the person, or not sent to them at all.

A person's list (console, MCP `list_actionable`, `ci actionable`) is step 2's confident "yes". Its unsure answers are listed separately as **possible**. Someone without a profile falls back to a simpler rule: named by the classifier, or the email was sent to them alone.

Set up Chuck Sr. once (console: **Action items → People with a person check**, or Cloud Shell). Give every company address of his (one `--address` each; not the `chuckjr` one), and say who he is not:

```bash
ci people set chuck_sr --name "Chuck Sr." --address chuckc@finishesetc.net --not-to-confuse "Chuck Jr., whose email address contains chuckjr. Requests addressed to Chuck Jr. or sent to his chuckjr address are not Chuck Sr.'s, even when the email just says Chuck."
```

Only Chuck Sr. has a list for now; Chuck Jr. has no profile of his own.

Changing a profile only affects mail processed afterwards. To apply it to mail already processed, run `ci process --remask --limit 0` (this re-runs everything, including the classifier).

### Properties, projects and legal entities

The classifier also returns the **properties/projects** an email is about (up to 5) and the **legal entities** it names (up to 8: LLCs, LPs, trusts, funds, companies), exactly as written in the email or its attachments, never inferred. They appear in Messages, the Label check, the Lab, and agent results (`properties_ai`, `entities_ai`). Filter by them in **Messages** (Property, Entity boxes) or from agents (`search_email` / `list_email` with `property` or `entity`): a partial, case-insensitive match, so `property=willow` finds "Willow Creek Retail".

Without a list, names appear as each email writes them ("WC Retail", "Willow Creek"). When you have the official lists, add them once; the classifier then prefers the canonical name, and aliases are mapped to it:

```bash
ci names add property "Willow Creek Retail" --alias "WC Retail" --alias "Willow Creek"
```

```bash
ci names add entity "Capital Company LLC"
```

`ci names list` shows them, `ci names remove property "Willow Creek Retail"` removes one. New names apply to mail processed afterwards; `ci process --remask --limit 0` re-runs earlier mail.

**Deploying this change:** it adds tables and columns (migrations `0008` to `0011`). Apply them **before** merging, or the new code fails on every processing run until they exist:

```bash
cd ~/fs-communication-intelligence && git fetch origin && git checkout matthew/nice-lovelace-4sqyit && git pull --ff-only origin matthew/nice-lovelace-4sqyit && make azure-migrate AZ_PSQL=psql && git checkout main
```

Then merge, set up the profile as above, and re-run the processed mail with `ci process --remask --limit 0`.

## Deploys: only what changed

A merge to `main` deploys only the apps its changes affect (`scripts/deploy-plan.sh`):

- Console only (`apps/admin`, `packages/ci_admin`): the admin app.
- `apps/ingestion` or `apps/search`: that app.
- Shared packages, `pyproject.toml`, `uv.lock` or the build script: all three apps.
- Docs, tests, scripts and workflow edits: nothing.

To deploy by hand: GitHub → **Actions** → **Deploy (development)** → **Run workflow**, then pick `all`, `ingestion`, `search` or `admin`. The test workflow (CI) also skips changes that only touch docs or Markdown files.

## What isn't done yet

- Per-folder delta sync and new-mail notifications (the hourly window is the stopgap); deletions and moves after capture aren't tracked.
- REST endpoints (MCP only for now), dead-letter alerts and dashboards, and infrastructure as Bicep (these scripts are the interim).
- monday.com Executive Hub feed.
- OCR and malware scanning for attachments (the extraction and original-file exceptions to L11 still apply).
