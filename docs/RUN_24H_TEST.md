# Run: Last 24 Hours of Company Email

Goal: capture every mailbox's mail from the last 24 hours, extract attachment text, screen and classify it, make it searchable, and produce Chuck's list of actionable items and risks, from Claude (MCP) or a Markdown report.

Status: the code is tested end to end against the synthetic fixture tenant (`make test`). **None of it has run against the real tenant or the Azure services yet.** This run is that first test, so expect to fix a few things.

Decisions behind this run (Matthew, 2026-10-28; recorded in `CLAUDE.md` §3a):
- All mailboxes are in scope. Specific mailboxes can still be kept out (`CustomAttribute15 = FS-CI-Exclude` in Exchange, and `ci scope exclude`).
- Data lives in the **Azure dev** PostgreSQL server `psql-fs-ci-dev-1026b`.
- Attachment text is extracted **without malware scanning or OCR** (an exception to L11). Scanned PDFs and images yield no text yet.
- The run uses a **test app registration with a certificate on your Mac**, not a managed identity. That certificate can read every in-scope mailbox. Treat it like a master key (step 7 removes it).

Do the steps in order. Step 1 first: Exchange can take **up to about 2 hours** to apply the permission.

## 0. Tools on the Mac (once)

```bash
brew install azure-cli
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Docker Desktop must be running (the migrations use its `psql`). Then sign in and install the project:

```bash
az login
```

```bash
cd ~/fs-communication-intelligence && git pull && uv sync
```

## 1. Mail access (start this first)

### 1a. Certificate (on the Mac; stays out of the repo)

```bash
mkdir -p ~/.fs-ci && chmod 700 ~/.fs-ci && cd ~/.fs-ci && openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=fs-ci-email-test" -keyout ci-graph.key -out ci-graph.crt && cat ci-graph.key ci-graph.crt > ci-graph.pem && chmod 600 ci-graph.* && cd -
```

The certificate expires in 30 days on purpose.

### 1b. App registration with only `User.Read.All` (lists users; no mail permission in Entra)

```bash
APP_ID=$(az ad app create --display-name "FS CI Email Ingestion Test" --query appId -o tsv) && SP_ID=$(az ad sp create --id $APP_ID --query id -o tsv) && az ad app credential reset --id $APP_ID --cert @$HOME/.fs-ci/ci-graph.crt --append >/dev/null && ROLE=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query "appRoles[?value=='User.Read.All'].id" -o tsv) && az ad app permission add --id $APP_ID --api 00000003-0000-0000-c000-000000000000 --api-permissions $ROLE=Role && echo "APP_ID=$APP_ID SP_ID=$SP_ID"
```

Write down `APP_ID` and `SP_ID`. Grant admin consent (needs a Global or Privileged Role Administrator):

```bash
az ad app permission admin-consent --id $APP_ID
```

**Never add `Mail.Read` here** (CLAUDE.md L5). Mail access comes only from Exchange, next.

### 1c. Exchange: read-only mail access to all user and shared mailboxes

In **Azure Cloud Shell → PowerShell** (portal.azure.com, the `>_` icon) or PowerShell on the Mac, as an Exchange administrator. Replace the two IDs:

```powershell
Connect-ExchangeOnline
$AppId = 'APP_ID-FROM-1B'
$SpId  = 'SP_ID-FROM-1B'
New-ServicePrincipal -AppId $AppId -ObjectId $SpId -DisplayName 'FS CI Email Ingestion Test'
$Filter = "(RecipientTypeDetails -eq 'UserMailbox' -or RecipientTypeDetails -eq 'SharedMailbox') -and -not (CustomAttribute15 -eq 'FS-CI-Exclude')"
New-ManagementScope -Name 'FS-CI-Email-All' -RecipientRestrictionFilter $Filter
New-ManagementRoleAssignment -Name 'FS-CI-Email-All-Read' -App $SpId -Role 'Application Mail.Read' -CustomResourceScope 'FS-CI-Email-All'
```

To keep a mailbox out, tag it **before** the run (and later run `ci scope exclude` for it too):

```powershell
Set-Mailbox -Identity 'someone@finishessolutions.com' -CustomAttribute15 'FS-CI-Exclude'
```

Check one included and one excluded mailbox:

```powershell
Test-ServicePrincipalAuthorization -Identity $SpId -Resource 'chuck@finishessolutions.com'
Test-ServicePrincipalAuthorization -Identity $SpId -Resource 'EXCLUDED@finishessolutions.com'
```

Expect `InScope = True` for the first and `False` for the second. Permissions can take up to about 2 hours to work in Graph calls. Continue with steps 2 to 4 while you wait.

## 2. Azure PostgreSQL (dev server)

```bash
RG=fs-communication-intelligence; PG=psql-fs-ci-dev-1026b; ME=$(az ad signed-in-user show --query id -o tsv); MYIP=$(curl -s https://api.ipify.org)
```

```bash
az postgres flexible-server parameter set -g $RG -s $PG --name azure.extensions --value VECTOR,PGCRYPTO
```

```bash
az postgres flexible-server firewall-rule create -g $RG -n $PG --rule-name matthew-mac --start-ip-address $MYIP --end-ip-address $MYIP
```

```bash
az postgres flexible-server microsoft-entra-admin create -g $RG -s $PG --display-name "$(az ad signed-in-user show --query userPrincipalName -o tsv)" --object-id $ME
```

If that command name is not found, set the admin in the portal: server → **Security → Authentication** → allow Microsoft Entra authentication → **Add Microsoft Entra Admins** → yourself. Then create the database:

```bash
az postgres flexible-server db create -g $RG -s $PG -d ci
```

## 3. Azure AI services

Two model deployments in `aoai-fs-ci-dev-1026` (portal: open the resource → **Go to Azure AI Foundry portal** → **Deployments** → **Deploy model**):
- `text-embedding-3-large`. Name the deployment exactly that.
- A chat model that supports structured outputs, such as `gpt-4.1-mini`. Note the deployment name.

Give each deployment enough tokens-per-minute capacity (start with about 100K for chat and 120K for embeddings). Too little capacity makes processing slow, not wrong.

Allow yourself to call the three services with your Entra sign-in:

```bash
for R in aoai-fs-ci-dev-1026:"Cognitive Services OpenAI User" lang-fs-ci-dev-1026:"Cognitive Services User" safe-fs-ci-dev-1026:"Cognitive Services User"; do NAME=${R%%:*}; ROLE=${R#*:}; az role assignment create --assignee $ME --role "$ROLE" --scope $(az cognitiveservices account show -g $RG -n $NAME --query id -o tsv) >/dev/null && echo "ok $NAME"; done
```

Show the exact endpoints for the config file:

```bash
for N in aoai-fs-ci-dev-1026 lang-fs-ci-dev-1026 safe-fs-ci-dev-1026; do az cognitiveservices account show -g $RG -n $N --query properties.endpoint -o tsv; done
```

## 4. Configure and prepare the database

```bash
cp local/azure-dev.env.example local/azure-dev.env && open -e local/azure-dev.env
```

Fill in every `<...>` value: tenant ID (`az account show --query tenantId -o tsv`), `APP_ID`, the certificate path (`/Users/<you>/.fs-ci/ci-graph.pem`), your sign-in for `PGUSER`, the endpoints and the chat deployment name. Then apply the schema:

```bash
make azure-migrate
```

It should end with `pgvector 0.8.x`. Set up a short alias for the rest of the run:

```bash
alias ci='uv run --env-file local/azure-dev.env ci'
```

## 5. Run

Register the tenant, read the mailbox list, and review it. Mailboxes outside the Exchange scope show `no_exchange_access`; that's expected for excluded ones:

```bash
ci tenant init && ci inventory sync && ci scope list
```

If `ci scope list` shows `no_exchange_access` for mailboxes that should be included, Exchange hasn't applied the permission yet. Wait and run `ci inventory sync` again.

Keep anyone out (repeat per mailbox), then approve every reachable mailbox and give yourself read access:

```bash
ci scope exclude someone@finishessolutions.com --reason personal_mailbox
```

```bash
ci scope include --all-enrolled && ci grant add --me && ci mode show
```

Capture the last 24 hours:

```bash
ci capture --hours 24
```

Process a small batch first and check it before processing the rest:

```bash
ci process --limit 25 && ci stats
```

If `failed error codes` lists anything, stop and send me the `ci stats` output (it contains codes and counts only, no email content). Otherwise process the rest:

```bash
ci process
```

```bash
ci stats
```

Re-running `ci capture --hours 24` later only adds new or changed mail. `ci process --retry-failed` retries failures.

## 6. Use it

Chuck's actionable list as a Markdown file. `local/out/` is git-ignored; it contains real email content, so don't commit or forward it carelessly:

```bash
mkdir -p local/out && ci actionable --person chuck@finishessolutions.com --hours 24 --out local/out/chuck-actionable.md && open local/out/chuck-actionable.md
```

Search from the terminal:

```bash
ci search "change order Three Lakes"
```

**Claude Desktop (MCP).** Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and add this (replace `YOU`; `which uv` and `which az` show the real paths), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "fs-communications": {
      "command": "/Users/YOU/.local/bin/uv",
      "args": ["run", "--directory", "/Users/YOU/fs-communication-intelligence",
               "--env-file", "local/azure-dev.env", "ci", "mcp"],
      "env": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    }
  }
}
```

Ask, for example: *"Use list_actionable for chuck@finishessolutions.com over the last 24 hours and summarise what needs his decision, with links."* Claude Code works too:

```bash
claude mcp add fs-communications -- uv run --directory "$PWD" --env-file local/azure-dev.env ci mcp
```

This MCP server runs on your Mac as **you**. It sees only what `ci grant add` allowed, through the same authorization as the planned API. Chuck using it from his own Claude needs the hosted endpoint with Entra sign-in (PR 7). Until then, share the Markdown report or run the questions for him.

## 7. After the test

- Keep the data only as long as needed. Stop capture by pausing (`ci mode set paused`; search returns nothing while paused).
- Remove mail access when the test ends:

```powershell
Remove-ManagementRoleAssignment -Identity 'FS-CI-Email-All-Read'
```

```bash
az ad app delete --id $APP_ID && rm -P ~/.fs-ci/ci-graph.*
```

- Remove your IP firewall rule when you're done:

```bash
az postgres flexible-server firewall-rule delete -g $RG -n $PG --rule-name matthew-mac --yes
```

## What to measure (CLAUDE.md §11)

`ci stats` gives processing success (published, held by reason, failed) and label counts. Report them as separate numbers, never as one "works 95%" figure. Before trusting labels, hand-check a sample: open 30 messages from `ci actionable` and 30 random published ones and note which labels were wrong. Probabilities are not accuracy.

## Known limits of this run

- Capture uses a 24-hour window listing, not continuous delta sync and webhooks (still PR 5). Deletions and moves after capture are not tracked.
- Shared and user mailboxes can't be told apart without another permission; inventory records them as `Other`.
- Attachments: text from PDF, Word, PowerPoint, Excel and text files. No OCR, no malware scanning, no `.doc`/`.xls`, no attached emails. Coverage shows in `ci stats` under `attachment extraction`.
- A message is restricted when Azure AI Language finds an SSN, tax ID, card, bank account, IBAN, driver's licence, passport or Medicare number with confidence ≥ 0.8. Invoices that print bank details for payment will be restricted; check `ci stats` for how many.
- Labels and thresholds are uncalibrated.
