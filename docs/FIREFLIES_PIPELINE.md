# Fireflies Meeting Pipeline (Second Source)

Status (2026-10-28): **built and tested against a synthetic workspace only.** Nothing has run against the real Fireflies account or in Azure yet; the first real capture is the first test of the API facts in §2. Decision recorded in [ADR-020](ARCHITECTURE_DECISIONS.md#adr-020-fireflies-meetings-are-the-second-source-without-pii-screening).

Matthew, 2026-10-28: *"pull Fireflies meetings from the whole company and have AI analysis done, no scanning for PII."* Source order is now **email → Fireflies → Teams**.

Related: [Deploy to Azure](DEPLOY_AZURE.md) · [Database](DATABASE.md) · [Search and Agents](SEARCH_AND_AGENTS.md)

## 1. What runs

```
func-fs-ci-ing-dev-1026 (the existing ingestion app; new timers, every 5 minutes)
  fireflies_capture  -> list meetings (team default + each member) -> exclusions -> fetch transcript
                        -> store meeting, participants, sentences, Fireflies summary (new version only if changed)
  fireflies_process  -> record "not screened" -> AI analysis -> chunk transcript -> embed -> publish atomically
```

| Piece | Where |
|---|---|
| Fireflies client (real GraphQL + synthetic fixture) | `packages/ci_fireflies/client.py`, `fixture.py` |
| Capture, exclusions, versioning | `packages/ci_fireflies/capture.py` |
| AI analysis (fake + Azure OpenAI) | `packages/ci_processing/meetings.py` (source-neutral; Teams meetings can reuse it) |
| Transcript chunking | `chunk_segments` in `packages/ci_processing/chunking.py` |
| Processing and publication | `packages/ci_fireflies/process.py` |
| Setup, scope and schedule | `packages/ci_fireflies/setup.py`, `ci fireflies ...` (`packages/ci_cli/fireflies.py`) |
| Timers | `apps/ingestion/function_app.py`, `packages/ci_cli/scheduled.py` |
| Schema | `db/migrations/0012_fireflies.sql` |
| Azure key setup | `scripts/azure/6-fireflies.sh` |

It runs in the **ingestion** Function App rather than a new one: that app's identity already has the database writer role and Azure OpenAI access, so no new identity, database login or deploy pipeline is needed. The Fireflies timers have their own kill switch (`FIREFLIES_ENABLED`) and schedule (`ci fireflies schedule`), separate from email.

## 2. Fireflies API facts used, and how sure we are

From Fireflies' published docs as summarized when this design was first written (2026-09-29) and from a web search on 2026-10-28. The docs site could not be fetched from the build session, so **none of these is verified against the live API**:

| Fact | Used for | If wrong |
|---|---|---|
| GraphQL at `https://api.fireflies.ai/graphql`, `Authorization: Bearer <key>` | every call | capture fails with `http_401`/`http_403` in `ci fireflies stats` |
| `users { user_id email name }` lists the team | per-member listing | `list_errors`; the default listing still runs |
| `transcripts(fromDate, toDate, limit ≤ 50, skip, user_id)`; `user_id` = meetings the user organized or attended | listing | GraphQL error code in the run counts |
| replies may be silently capped at 50 | a full page splits the time window in half (down to 30 minutes) | — |
| `transcript(id)`: `date` in epoch ms, `duration` in **minutes**, sentence times in **seconds**, `summary { overview short_summary action_items keywords }`, `meeting_attendees { displayName email name }` | normalization | wrong durations/times, or a GraphQL "field not found" error |
| Business/Enterprise: 60 requests/minute | the client paces itself to `FIREFLIES_RATE_LIMIT_PER_MINUTE` (default 50) and retries 429/5xx | slower runs, more retries |

**The main open question is whether one API key sees the whole company.** A key belongs to one Fireflies user. Capture lists the key's default view **and** each team member by `user_id`, then de-duplicates by transcript id. Whether an admin key may list other members' meetings this way is unconfirmed. After the first run, compare `found_via_default`, `found_via_user` and `workspace_users` in `ci fireflies stats` with the meeting count in the Fireflies dashboard. If per-user listing returns errors or nothing, use an admin (or dedicated service) seat's key, or ask Fireflies support how team-wide API access works on your plan.

## 3. Scope: the whole company, with people kept out

- One scope, kind `fireflies_workspace`, stands for the whole Fireflies team. `ci fireflies init` registers it, approves it for `test`, creates the corpus `meetings-enrolled` with a `Communications.Reader.All` grant, and enables the `fireflies` source in the active `runtime_config` revision.
- **Keep a person out:** `ci scope exclude --participant name@finishessolutions.com --reason CODE`. Any meeting where they were organizer, host or attendee is skipped at capture (nothing stored) and hidden from every published view if it was stored before. This is the same participant exclusion email uses, so it also hides their email in every mailbox. A whole domain: `--participant @example.com`.
- Fail closed: paused mode, the source not enabled, or no approved workspace → capture refuses (`refused_EmptyScopeError`). Paused mode also makes every published view empty.
- Production approval needs `ci fireflies init --production --confirm-production`. Production releases remain Matthew's call (CLAUDE.md §13).

## 4. Capture

For each listed meeting: item exclusion → fetch the transcript → participant exclusion → skip if it has no sentences yet (`not_ready`; Fireflies is still transcribing and a later run picks it up) → store.

- A meeting is one communication (`kind = 'meeting'`, native id = transcript id), linked to the workspace scope, with `fireflies_meetings` metadata (organizer, host, meeting link, sentence count; no content), participants (organizer, host, attendees; provider `fireflies`), one `transcript_segments` row per sentence, and Fireflies' own summary as `version_texts` (`provider_summary`).
- A new version is stored only when the normalized content changes: an edited transcript, a renamed speaker, or a summary that arrives later. Re-running a window stores nothing (`unchanged`).
- Every listed meeting is re-fetched on each run so late summaries and edits are seen. With the default 72-hour look-back and an hourly schedule that is one request per meeting of the last three days per run, well inside 50 requests a minute for a company this size.
- **Not done:** deletions in Fireflies are not detected (no deletion webhook exists; a reconciliation sweep is future work), and webhooks are not used (polling only).

## 5. Processing and AI analysis

1. A `policy_decisions` row records `pii / allow / not_screened_by_policy`, so the absence of screening is visible in the data.
2. **AI analysis** (Azure OpenAI chat deployment in the Finishes tenant, JSON-schema output): a neutral summary, decisions made, action items (task, owner as named, owner email only when it matches an attendee, due as stated), project risks (the same definition as email `project_risk`), property/project and legal-entity names as spoken (mapped through the `ci names` registry), and these labels with the provisional §7 thresholds: `confidential_business`, `project_related`, `project_risk`, `urgent` and the seven `cat_*` categories. Stored in `meeting_analyses`; identical input is never re-analyzed. Transcripts over 120,000 characters (roughly two hours or more) are cut and the analysis is marked `input_truncated`.
3. **Chunks:** transcript windows of whole sentences (~1,400 characters, a small overlap, never splitting a sentence) as `"Speaker: words"` lines, each citing its sentence range, start/end time and speakers; plus the Fireflies summary as `provider_summary` chunks. The embedding input adds a header (meeting title, date, organizer, speakers) that is not shown as evidence.
4. **Publish** in one transaction (new version published, prior superseded, active version moved), exactly like email. Paused mode just before publishing fails the version, to be retried.

All of it is a proposal, never an approval. Thresholds are uncalibrated.

## 6. Who can read it, and what is not built yet

- The `published_meetings` and `published_meeting_analyses` views exist and the `ci_search` role can read them, with every mode, scope and exclusion rule applied.
- **Not built yet:** MCP/REST tools for meetings (`search_meetings`, `get_meeting`) and an admin-console page. Email search (`search_email`) explicitly returns email only, so meetings never displace email results. Until meeting tools exist, the data is reachable only through the database.

## 7. Turn it on (dev)

Cloud Shell, repository folder, with `local/azure-dev.env` from [DEPLOY_AZURE.md §5](DEPLOY_AZURE.md#5-database) and `alias ci='uv run --env-file local/azure-dev.env ci'`.

1. Merge the pull request (GitHub Actions deploys all three apps; the Fireflies timers stay off).
2. Apply the new migration (`0012_fireflies.sql`):

   ```bash
   git pull && make azure-migrate AZ_PSQL=psql
   ```

3. Store the Fireflies API key in Key Vault and point the ingestion app at it. Use the key of a Fireflies **admin** seat (Fireflies → Settings → Developer settings). The script asks for it without echoing it:

   ```bash
   bash scripts/azure/6-fireflies.sh
   ```

   Check: the app's **Environment variables** page shows a green Key Vault reference tick on `FIREFLIES_API_KEY`.

4. Register the workspace, keep people out, and ask for a first capture of the last 7 days. The capture starts at the next 5-minute tick:

   ```bash
   ci fireflies init && ci scope exclude --participant someone@finishessolutions.com --reason hr_private
   ```

   ```bash
   az functionapp config appsettings set -g fs-communication-intelligence -n func-fs-ci-ing-dev-1026 --settings FIREFLIES_ENABLED=true -o none && ci fireflies run-capture --hours 168
   ```

5. About 10 minutes later, check counts (codes and counts only):

   ```bash
   ci fireflies stats
   ```

   Compare `listed` with the Fireflies dashboard (§2). `list_errors` or `error_fetch` with a code means a fact in §2 was wrong: send that line.

6. Turn on hourly capture with a 72-hour look-back (transcripts appear some time after a meeting ends):

   ```bash
   ci fireflies schedule set --on --every 60 --lookback 72
   ```

   Backfill further: `ci fireflies run-capture --since 2026-09-01` (up to 93 days).

Stop: `ci fireflies schedule set --off`, or the hard switch `FIREFLIES_ENABLED=false`. `ci mode set paused` stops every source.

Locally (no Azure, synthetic workspace): `make dev`, then `uv run --env-file local/local.env ci fireflies init`, `... ci fireflies capture --hours 2232`, `... ci fireflies process`.

## 8. Decisions still open

| ID | Decision | Current behavior |
|---|---|---|
| FF-D1 | Which seat owns the API key | Whoever's key is stored; an admin seat is recommended (§2) |
| FF-D2 | Meetings to keep out beyond named people (HR, board, 1:1s, by title) | Only participant and item exclusions. No title rules. **Because there is no PII screening, HR or medical conversations are indexed and readable by everyone with `Communications.Reader.All`.** |
| FF-D4 | Store audio/video | No |
| FF-D7 | What the existing Fireflies → Zapier integration does | Not inventoried; this pipeline writes nothing back to Fireflies or elsewhere |
| FF-D8 | Retention | Not implemented (no automatic deletion), same as email (D-R1) |
| — | Who may read meetings | Same `Communications.Reader.All` role as all company email |
