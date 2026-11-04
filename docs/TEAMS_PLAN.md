# Microsoft Teams: discovery and build plan

Written 2026-11-04 while the 2025 email backfill was running. Status: **proposed**. Nothing here is built, and the decisions in §4 are Matthew's to make before T1 starts. Where this plan says a Microsoft fact is "verified", it was read on learn.microsoft.com on 2026-11-04 and the page is linked; where it says "reported" or "assumed", it is not, and T0 exists to find out.

Read with `CLAUDE.md` (the decisions it records still apply) and ADR-022.

---

## 1. Summary

Teams is the second source (ADR-014). The shared packages (`ci_core`, `ci_processing`, `ci_retrieval`), the database foundation (`0001`, which already has `teams_chat`/`teams_channel` scope kinds and `chat_message`/`channel_message` communication kinds) and the hosted apps were built so Teams could reuse them. Most of the work is a new `ci_teams` package that does for chats and channels what `ci_email` does for mailboxes, plus small extensions to processing, retrieval, the console and the schema.

**Almost no new Azure infrastructure is needed.** The same PostgreSQL, Azure OpenAI, Language, Content Safety, Function Apps and storage carry Teams. What is new is **Entra permissions**, and that is where Teams differs most from email:

| | Email (today) | Teams |
|---|---|---|
| Hard boundary | Exchange Application RBAC scopes the identity to named mailboxes (L5). The identity *cannot* read an excluded mailbox. | **No equivalent exists for chats.** Reading 1:1 and group chats without a user requires the tenant-wide application permission `Chat.Read.All`; channels require `ChannelMessage.Read.All`. The identity *can* read every chat in the tenant; the database decides what it *does* read. |
| Microsoft approval | None beyond admin consent. | These are "protected" Teams APIs. Historically Microsoft had to approve the app through a request form (days to weeks). Whether that is still enforced for every tenant is **not verified** (§2.3); T0 tests it with one call. |
| Cost per API call | Free. | Free. Teams message APIs stopped being metered on 2025-08-25 (verified). |
| Identity for a stored item | One copy **per mailbox** (each mailbox has its own permissions). | One copy **per message** (a chat's permissions are its membership). |
| Unit the executive cares about | One email. | A **conversation window** (a channel post with its replies; a run of chat messages), not one line of chat. |
| Attachments | In the message; extracted. | **Links to SharePoint/OneDrive files**; fetching the file is a separate, tenant-wide permission. Pasted images are "hosted content" on the message. |

Recommended shape: add Teams behind the existing ingestion app with a **separate managed identity** that holds the Teams permissions and nothing else, capture by **time window per user and per team** through the Teams export APIs (the same shape as today's email capture, no webhooks yet), store one communication per message, classify and embed per **conversation window**, and serve through the same authorized retrieval code with new MCP tools.

---

## 2. What Microsoft Graph offers (verified unless marked)

### 2.1 Reading messages without a signed-in user

| Need | API | Application permission | Notes |
|---|---|---|---|
| All messages in all chats a user is in (1:1, group, meeting chats) | `GET /users/{id}/chats/getAllMessages` ([doc](https://learn.microsoft.com/graph/api/chats-getallmessages?view=graph-rest-1.0)) | `Chat.Read.All` | Export API. `$filter` on `lastModifiedDateTime` range, by sender, by sender type (`federatedUser`, `aadUser`), excluding `systemEventMessage`. Returns sent *and* received, so the same message appears under every member: dedupe by message id. Order is not guaranteed. `$top` up to 250 recommended; a page may be short and still have a next link. |
| All messages in all channels of a team (standard, private and shared channels) | `GET /teams/{id}/channels/getAllMessages` ([doc](https://learn.microsoft.com/graph/api/channel-getallmessages?view=graph-rest-1.0)) | `ChannelMessage.Read.All` | Export API, same filters. Replies included. |
| Changes since last time, per user | `GET /users/{id}/chats/getAllMessages/delta` ([doc](https://learn.microsoft.com/graph/api/chatmessage-delta?view=graph-rest-1.0)) | `Chat.Read.All` | **Only the last eight months**; older history must come from `getAllMessages`. `$top` max 50. |
| Changes since last time, per channel | `GET /teams/{id}/channels/{id}/messages/delta` | `ChannelMessage.Read.All` | Per channel, so one checkpoint per channel. |
| Pasted images, code snippets | `.../messages/{id}/hostedContents/{id}/$value` ([doc](https://learn.microsoft.com/graph/api/chatmessage-list-hostedcontents?view=graph-rest-1.0)) | same | Not needed for text. |
| Files shared in a message | `attachments[]` with `contentType = reference` and a SharePoint/OneDrive `contentUrl` ([doc](https://learn.microsoft.com/graph/teams-messaging-overview#chatmessage-schema)) | `Files.Read.All` or `Sites.Read.All` to fetch bytes | The file is not on the message. v1 records the link only (§5.5). |
| Who is in a chat | `GET /chats/{id}/members` | `ChatMember.Read.All` (or `Chat.Read.All`) | Needed for authorization (§6). |
| Which teams and channels exist | `GET /teams`, `GET /teams/{id}/channels` | `Team.ReadBasic.All`, `Channel.ReadBasic.All` (names to confirm in T0) | Inventory. |
| Which users exist | `GET /users` | `User.Read.All` | Already granted to the ingestion identity. |

Also verified from the export API page ([doc](https://learn.microsoft.com/microsoftteams/export-teams-content)):
- Messages a user deleted stay readable through the export APIs for **21 days** with `deletedDateTime` set, so a windowed capture on `lastModifiedDateTime` sees edits and deletions without webhooks.
- Deleted teams/channels and deleted or inactive users remain exportable for 30 days.
- Requires an active Teams license on the users whose data is exported. No Purview or DLP licence is needed.
- Export API filters let a per-user call return only messages *sent by* that user, which would read each message once instead of once per member. Not recommended for v1: messages from external people and bots would be missed unless a second pass runs.

### 2.2 Real-time change notifications (later phase)

Tenant-level subscriptions exist: `/chats/getAllMessages` and `/teams/getAllMessages`, one each, same permissions ([doc](https://learn.microsoft.com/graph/teams-changenotifications-chatmessage#subscribe-to-changes-at-the-tenant-level)). Constraints: a Teams subscription lasts at most **60 minutes** and must be renewed; above one hour a `lifecycleNotificationUrl` is mandatory; notifications can carry the message body encrypted to a certificate we own (`includeResourceData`) or just the id (then we fetch it). Recommended for a later PR (T6), not v1. With ids only, no certificate is needed and the webhook stays as thin as the email one in CLAUDE.md §6.2.

### 2.3 Protected APIs and Microsoft approval (not verified)

The export API page says these APIs "are considered protected APIs" and can be called "as long as the requirements for accessing without a user are met". The page that lists the request process (`graph/teams-protected-apis`) could not be fetched on 2026-11-04. Microsoft Q&A threads from 2025 show both outcomes: one app got `403 "Invoked API requires Protected API access in application-only context"` even after admin consent and had to file the form; another found 1:1 chats readable with `Chat.ReadWrite.All` alone. **T0 settles this for our tenant with one real call.** If the form is required, approval is measured in days to weeks and is the critical path; submit it on day one.

### 2.4 Throttling (verified, [doc](https://learn.microsoft.com/graph/throttling-limits#microsoft-teams-service-limits))

| Call | Per app per tenant | Per resource |
|---|---|---|
| `getAllMessages` (chats or channels) | 200 requests/s | – |
| `GET /chats/{id}/messages` | 20 requests/s | 1/s per chat |
| `GET /teams/{id}/channels/{id}/messages` | 20 requests/s | 1/s per channel |
| `hostedContents/$value` | 60 requests/s | 1/s per chat |
| Anything else (delta, members, inventory) | 30 requests/s | 1/s per chat or channel |

A sustained rate near 83% of a limit is also throttled, so plan for well under. Compared with mail (four concurrent requests per mailbox), the export path is generous: the per-user export call is the right primary path, and the per-chat and per-channel calls should be used only for repairs.

### 2.5 Known limits

- Chats hosted by **another tenant** (an external person started the chat) are reported as not readable with application permissions (`403 AclCheckFailed` in a 2025 Q&A thread). Chats hosted here that include external people are exportable (the API has a `federatedUser` filter). Treat as reported; T0 tries one of each if available.
- A tenant has at most 10,000 Teams change-notification subscriptions in total. Irrelevant for v1, relevant if per-chat subscriptions were ever considered (they should not be).
- Message bodies are HTML with `<at>` mention tags, attachment placeholders and card JSON. Normalisation is more work than email: mentions become names, cards become their text, system event messages are dropped.

---

## 3. Azure and Entra: what is new

### 3.1 New

| Item | Why | Script |
|---|---|---|
| **User-assigned managed identity `id-fs-ci-teams-dev`** in `fs-communication-intelligence`, attached to `func-fs-ci-ing-dev-1026` as a second identity | Keeps the tenant-wide Teams read permissions off `id-fs-ci-ingestion-dev`, so the mail identity still has no tenant-wide Graph read (L5 stays literally true for mail) and the Teams identity has no mailbox, Blob or Key Vault access. The code picks it by client id (`AZURE_TEAMS_CLIENT_ID`). Honest limit: both identities run in one process, so this is separation of grants, not of processes. | `scripts/azure/10-teams-access.sh` (new) |
| **Graph application permissions on that identity**: `Chat.Read.All`, `ChannelMessage.Read.All`, `ChatMember.Read.All`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`, `User.Read.All` | §2.1. Granted as app role assignments, the same way `3-mail-access.sh` grants `User.Read.All`; needs a Privileged Role or Application administrator, run as `svc_automation@`. No `*.ReadWrite.*`, no `Files.*` in v1. | same script |
| **Microsoft protected-API approval** (maybe) | §2.3. Form, not infrastructure. | manual |
| **App settings** on the ingestion app: `TEAMS_CAPTURE_ENABLED=false`, `AZURE_TEAMS_CLIENT_ID`, `TEAMS_BACKFILL_SINCE` (empty = blocked, like email) | Hard kill switch and fail-closed boundary, same pattern as capture today. | `1-function-apps.sh` (extend) + deploy workflow |
| **Database migration `0027_teams.sql`** | §5.1. Same server. | `db/migrate.py` |

### 3.2 Reused unchanged

PostgreSQL `psql-fs-ci-dev-1026b` (check size and tier before backfill, §7), Azure OpenAI `aoai-fs-ci-dev-1026` (same chat and embedding deployments; the per-minute token quota is shared with email processing, so a Teams backfill competes with the email one), Language and Content Safety (same masking and Prompt Shields path), App Insights, Key Vault, the three Function Apps, the search API registration, app roles and the MCP broker. No new storage account or container: v1 fetches no files.

### 3.3 Not needed

Service Bus, Event Grid, a Teams app package in the Teams admin center (only needed for the resource-specific-consent route, §4 D-T1 option B), a certificate for encrypted notifications (only with `includeResourceData`, not recommended), Purview or any licence beyond Teams.

### 3.4 Cost

Graph: $0 (not metered since 2025-08-25, [FAQ](https://learn.microsoft.com/graph/metered-api-faq)). The paid steps are the same as email: Prompt Shields, PII masking, classification and embeddings, metered into `ai_usage` with a `source` column so the Costs page separates Teams from email. Chat is many short messages; processing per window (§5.4) keeps the number of model calls close to the number of conversations, not messages. **No volume estimate is possible yet.** Where to get the counts (needs a Teams administrator or Reports reader role; a Global reader sees totals but not per-user rows, so sign in as `svc_automation@` if Matthew's own account is refused):

- **Microsoft 365 admin center → Reports → Usage → Microsoft Teams → User activity tab**: https://go.microsoft.com/fwlink/p/?linkid=2074756 . Columns `Chat messages` (private chat messages the user posted) and `Channel messages` (posts plus replies). Pick the longest range offered (up to 180 days), export to CSV, sum both columns across users. That sum is messages posted, so it is the number of messages to capture over that period, scaled to the backfill period. ([report definitions](https://learn.microsoft.com/microsoft-365/admin/activity-reports/microsoft-teams-user-activity-preview))
- **Teams admin center → Analytics & reports → Usage reports → Teams usage**: https://admin.teams.microsoft.com/analytics/reports . Messages per team and active channels, which tells us how many teams and channels T1 will enrol.
- **Files**: there is no report of files shared in chats. **SharePoint site usage** (same Usage page) gives file counts per team site, which is channel files only; chat files live in each sender's OneDrive and show up only in **OneDrive usage** totals. Treat file counts as unknown until T2's first capture records `teams_attachments` for a real window.

Then multiply by the per-email figures in `docs/DEPLOY_AZURE.md` "Backfill a long period" as a first guess, remembering that a window (§5.4) bundles many messages into one classification call. Record the numbers in this file when known.

---

## 4. Decisions needed from Matthew (implement as configuration, defaults shown)

| ID | Question | Options | Default until decided |
|---|---|---|---|
| **D-T1** | Accept a **tenant-wide** application permission for chats? There is no Exchange-style scope for chats. | **A (recommended):** `Chat.Read.All` + `ChannelMessage.Read.All` on `id-fs-ci-teams-dev`; the database scope list and exclusions decide what is captured; every grant and every captured scope is audited. **B:** resource-specific consent per team (`ChannelMessage.Read.Group`) through a Teams app installed in each team: a real hard boundary for *channels*, no coverage for 1:1 or group chats, and one install per team. **C:** channels now via B, chats never. | Nothing is granted until decided. T0 cannot start without A or B. |
| **D-T2** | Which people and teams? | Same rule as email: **all users and all teams**, with named exceptions (`ci scope exclude`). Excluding a *user* stops capture through that user's chat list; a chat they share with an in-scope user is still captured through that user (same as email's "an excluded mailbox does not exclude that person's mail in other mailboxes"). Excluding a *chat* or *team* removes it entirely. | All users and teams, `TEAMS_INCLUDE_MEETING_CHATS=true`. |
| **D-T3** | Processing unit. | **Window (recommended):** a channel post with its replies; for chats, consecutive messages with gaps under `TEAMS_WINDOW_GAP_MINUTES` (60), capped at 40 messages or 8,000 characters. Classification, action items, risks and embeddings run per window; citations still name the individual message. **Per message:** simpler, but "ok" and "see attached" become classification calls and action items lose their context. | Window. |
| **D-T4** | Files shared in Teams. **A link-only file is not readable by the AI**: people can open it, search cannot see inside it. | **Links only (v1 default):** record name, size, type and the SharePoint link; `get_file_links` returns them; results say "N files not read". **Fetch text-bearing files (recommended once T2 works, Matthew 2026-11-04 asked whether links are readable):** `Files.Read.All` (tenant-wide: every OneDrive and SharePoint site, read-only) on the Teams identity, download only the types the email extractor already handles (PDF, Word, PowerPoint, Excel, text, HTML) under `TEAMS_FILE_MAX_BYTES` (say 25 MB), through the same sandboxed extractor and the same unscanned-file exception as email; everything else (zip, video, audio, images, CAD, large files) is recorded as metadata and a link, exactly as email treats them today. Pasted screenshots are hosted content and need OCR (E2) before they mean anything. | Links only until `Files.Read.All` is approved; then the fetch rule above (T7). |
| **D-T5** | Backfill boundary for Teams. | Any date; the export APIs have no eight-month limit (delta does). | `TEAMS_BACKFILL_SINCE` empty = blocked, like email. Suggest `2025-01-01` to match. |
| **D-T6** | Who may read Teams evidence. | `Communications.Reader.All` would see **everyone's private 1:1 chats**, which is a larger privacy step than mailboxes. Options: same roles as email (simplest); a separate `Communications.Teams.All` app role so a person can hold email-all without chats-all; reader limits (`reader_restrictions`) extended to Teams in either case. | Same roles, reader limits extended. Flag in Chuck's access review. |
| **D-T7** | Edits and deletions. | Edit = new version (prior superseded). Delete = withdrawn (hidden first, purged later), matching the email lifecycle. Alternative: keep deleted text for 21 days as Teams itself does. | Withdraw on delete. |

---

## 5. Design

### 5.1 Schema (`0027_teams.sql`, additive)

- `source_connections`: one row `provider = 'teams'`, `account_ref` = tenant id.
- `source_scopes`: three kinds in use. `teams_user` (new kind, native id = Entra object id, the unit chats are *captured through*), `teams_chat` (native id = chat id, the unit chats are *authorized by*), `teams_channel` (native id = `teamId/channelId`). The `0001` check constraint gains `teams_user`. `scope_state` reuses `enrolled`, `excluded_by_rule`, `not_approved`, `paused`; `no_exchange_access` becomes the general "403 from Graph" state (rename in the same migration, with the email code reading the new name).
- `teams_users` (scope id, UPN, display name, last inventory), `teams_chats` (scope id, chat type `oneOnOne|group|meeting`, topic, hosting tenant, last member sync), `teams_channels` (scope id, team id, team name, channel name, membership type `standard|private|shared`), `teams_chat_members` (chat scope id, user object id, UPN, added/removed timestamps, from `/chats/{id}/members`), `teams_channel_members` (same, from team or channel membership).
- `teams_messages` (communication id, chat or channel scope id, native message id, reply-to id, author object id and UPN, author kind `user|federated|bot|application|unknown`, created, last edited, deleted, `web_url`, mention count, attachment count, hosted content count, importance). `communications.kind` = `chat_message` or `channel_message`.
- `teams_attachments` (version id, attachment id, kind `file|card|messageReference|meeting|tab|other`, name, content type, `content_url` for files, size when known). No bytes.
- `teams_windows` (window id, scope id, first/last message time, message count, members at the time, version id of the derived window communication). The window is itself a `communications` row with `kind = 'chat_window'` (new kind) so processing, publication, classification, chunks, knowledge, risk ratings and resolutions work unchanged; `relationships` rows (`kind = 'thread'`) link it to its member messages.
- `version_texts.text_kind` gains `teams_window` and `teams_message`; `chunks.chunk_kind = 'chat_context'` is already allowed.
- `ops_runs.kind` gains `teams_capture`; `ops_backfill_windows` gains `source` (`email|teams`); `ai_usage` gains `source`.
- `published_*` views: `published_teams_messages` and `published_teams_windows` with citation fields, built on `published_communications` so exclusions apply.

### 5.2 Capture (`ci_teams`, mirrors `ci_email`)

- `graph.py`: a `TeamsGraphClient` protocol with a fixture implementation (`GRAPH_BACKEND=fixture`, synthetic chats, channels, members, pages, 410/429/403) and an httpx implementation using the Teams identity. Same content rule: never log bodies.
- `inventory.py`: list users (reuse the email inventory's user list), teams and channels; upsert scopes; apply exclusions; `ci teams scope list|include|exclude`.
- `capture.py`: for each enrolled user, `getAllMessages` with `lastModifiedDateTime` in the window, pages of 250; for each enrolled team the channel export. Per message: skip `systemEventMessage`; skip authors of kind `bot`/`application` unless `TEAMS_INCLUDE_APPS=true`; skip `policyViolation` messages; apply participant and domain exclusions on the author; record or update the chat/channel scope and its members (member sync at most once per hour per chat); normalise HTML to text (mentions to names, attachment placeholders to `[file: name]`, cards to their text); identity = dataset + connection + chat or channel + message id; new version only when normalised content changes; `deletedDateTime` set → withdraw. Duplicates across members are simply the same identity.
- Windowing runs at the end of each capture: new or changed messages dirty their window; a dirty window gets a new derived version (input hash dedupes identical content) and is queued for processing. Replies to a channel post always join the post's window.
- Windows and backfill reuse `ops_backfill_windows` with `source = 'teams'`; the scheduler ticks both sources, `TEAMS_CAPTURE_ENABLED` is the kill switch, and the database mode stops everything.

### 5.3 Screening and masking

Unchanged: Prompt Shields on the classifier's input, PII and credential masking before classification under `SENSITIVE_DATA_POLICY=mask`, the operator switches, `policy_decisions`. The prefilter gets Teams rules: system and bot messages, reaction-only and empty messages, messages that are only a file card.

### 5.4 Classification, chunking, embedding

- The classifier prompt gets a `source = teams` variant: no subject, a transcript-style window with `[time] Name: text`, chat type and members as context, the same label set. `approval_required`/`action_required` keep the "asks a named person" tightening; the person check (`action_people`) runs on windows.
- Chunks: one `chat_context` chunk per window segment (about 350 tokens, paragraph = message boundary), header = chat or channel name, members, dates; citations = the messages in the segment (message ids, author, time, `webUrl`).
- Knowledge, risk ratings and resolution checks run on windows unchanged. Resolution for Teams is natural: the next window in the same chat is the "reply".
- Label review and review examples work per window; Teams examples are shown to the Teams prompt only.

### 5.5 Attachments and images

What "links only" means: the system stores the file's name, type, size and SharePoint link. An executive or an agent gets the link and can open the file in SharePoint if they have access there. **The AI cannot read or search the file's contents.** A question like "what did the inspection report in the project channel say" is answered only from what people wrote in the chat around it.

What "fetch" would mean (D-T4 second option): the same as email attachments today. The email extractor reads PDF, Word, PowerPoint, Excel, text and HTML in an isolated child process with size, time and zip-bomb limits, and marks everything else `unsupported` or `too_large`. Zips, videos, audio and images are never opened by either path; large files hit the size cap and stay links. So the question is not "will zips and videos be read" (no, in either option) but "will PDFs, Word and Excel files shared in Teams be searchable" (only with `Files.Read.All`).

Either way, `get_file_links` returns `contentUrl` as the file link (it opens for anyone with SharePoint access, unlike Outlook links) and the console read page as `view_url`. The coverage block always reports how many files were not read and why.

### 5.6 Lifecycle

Edit → new message version and a dirty window. Delete (`deletedDateTime`) → message withdrawn, window re-derived without it. User or team leaves scope → withdrawal of the messages captured *only* through that scope (a chat still reachable through another enrolled member stays). Chat membership changes → `teams_chat_members` rows closed, which changes who can read it from that moment (no retroactive hiding; recorded as a decision for Chuck's review).

---

## 6. Authorization and retrieval

- The one authorization fragment (`ci_retrieval.search._AUTH`) gains the Teams own-scope: a `Communications.Reader.Own` reader sees a chat or channel communication when `teams_chat_members`/`teams_channel_members` lists them (current membership). `Reader.All` sees all, subject to D-T6.
- `ci.reader_hidden()` is extended: `mailbox` limits map to the person's user scope; `person`, `domain` and `term` limits apply to authors and members; the "except mail the reader is on" exception becomes "except chats the reader is a member of". Will's existing limits carry over without new rows.
- Exclusions and withdrawal apply through `published_communications` as today.
- MCP tools (same validation, audit and 24,000-character fit): `search_teams`, `list_teams` (by chat, channel, team, author, label, property, entity, dates), `get_teams_window` (the window with its messages and links), `get_chat` (a chat's recent windows). `list_actionable`, `email_stats`, `risk_overview`, `find_knowledge` and `get_profile` take `source = email|teams|all` (default `email` until Matthew flips it, so Copilot prompts written for email keep their results). `search_email` stays email-only. Tool descriptions carry the same untrusted-content, cite-everything, empty-means-not-found wording.
- Links: `view_url` opens `/read/t/<id>` on the console (same roles as email links: `Communications.Admin` or `Communications.Files`); `web_url` from Graph opens the message in Teams for members.
- Console: **Mailboxes & Scope** gains a Teams tab (users, teams, channels, states, exclusions); the inbox-style list, Label check, Action items, Risk, Knowledge and Costs pages get a source filter; **Schedule & Runs** shows Teams captures and the Teams kill switch.

---

## 7. Volume, speed and the database

Unknown until the admin-center counts are in (§3.4). What is known: the export path allows 200 requests/s per app per tenant with 250 messages per page, so Graph is unlikely to be the bottleneck; model calls per window and the Azure OpenAI quota will be, exactly as for email. Before the Teams backfill: check `pg_database_size`, the server's storage tier and the HNSW index build time, because Teams adds chunks to the same index the email backfill is filling now. Run the Teams backfill **after** the email backfill finishes, never alongside it, unless the OpenAI quota is raised.

---

## 8. PR sequence

Each PR lists what was verified (tests run, commands executed) separately from what is assumed, as in CLAUDE.md §12.

| PR | Content | Done when |
|---|---|---|
| **T0 Access spike** (no app code) | `scripts/azure/10-teams-access.sh`: create `id-fs-ci-teams-dev`, attach it to the ingestion app, grant the §3.1 permissions; a Cloud Shell check that calls `getAllMessages` for one user and one team with the identity's token. ADR-022 updated with the outcome. | Either 200 with messages, or the exact protected-API error and the form submitted. Also: one cross-tenant chat attempt recorded. |
| **T1 Schema, inventory, scope** | `0027_teams.sql`, `ci_teams.inventory`, `ci teams scope`, console Teams tab (read-only), fixture client, synthetic fixtures (1:1, group, meeting chat, channel post with replies, edit, delete, bot message, external author, mention, file card, pasted image, prompt injection, SSN-like string). | Inventory on fixtures and on the real tenant; exclusions win; empty scope fails closed. |
| **T2 Capture** | Windowed export capture per user and per team, normalisation, dedupe, lifecycle, windowing, backfill windows with `source`, scheduler, kill switch. | Fixture tests for dupes, 410/429/403, edit, delete, membership change; one real 24-hour window captured into dev (same data rule as the email 24-hour run: firewall, delete after). |
| **T3 Processing** | Teams prefilter rules, prompt variant, window chunks, publish, knowledge/risk/resolution on windows, `ai_usage.source`. | Fixture run end to end; label review sample of 50 windows hand-checked (§11 metrics, per source). |
| **T4 Retrieval** | `_AUTH` and `reader_hidden` extensions, new MCP tools, `source` parameters, console read page and filters, deploy workflow. | Authorization negatives (§11) pass for Teams: wrong audience, missing role, non-member with Reader.Own, excluded chat, paused mode, forged filter. Claude, ChatGPT and Copilot each answer one Teams question with citations. |
| **T5 Backfill** | `ci backfill plan --source teams --since …`, run, measurement. | Counts per user and team versus the admin-center report; cost from the Costs page. |
| **T6 Notifications** (later) | Tenant-level subscriptions with 45-minute renewal, thin webhook, lifecycle events, delta repair for the last eight months. | Notifications and the windowed capture agree over a week. |
| **T7 Files** (only if D-T4 = fetch) | `Files.Read.All`, download by sharing link, extraction through the email extraction sandbox, Blob originals. | Same rules and exceptions as email attachments. |

---

## 9. Risks and unknowns

1. **Protected-API approval** (§2.3) may add weeks before T2 can run against real data. T0 first.
2. **Privacy step change.** Private 1:1 chats are more sensitive than mail; D-T6 and the access review must be settled before T4 ships to anyone but Matthew.
3. **Windowing quality is unmeasured.** The gap and size thresholds are guesses; the Label check gives the first numbers.
4. **Normalisation of cards, Loop components and mentions** will have gaps; unrecognised content is kept as its text or dropped with a `coverage` note, never silently.
5. **Cross-tenant chats** are reported unreadable; if a lot of business happens in chats started by outside parties, that coverage hole must be stated in results.
6. **Shared OpenAI quota** with the email pipeline (§7).
7. **Name collisions** in the knowledge base: Teams display names versus email addresses. The knowledge extractor keys people by UPN where it has one.
8. **`scope_state` rename** (`no_exchange_access` → a general name) touches email code and the console; do it in T1 with tests, or keep the old name and document it.
