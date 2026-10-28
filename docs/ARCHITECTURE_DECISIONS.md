# Proposed Architecture Decisions

Status: proposed for the first implementation release. These describe intended behavior, not features already implemented.

**Revision 2026-09-29:** Fireflies is now the first source (ADR-010). ADR-011 and ADR-012 simplify the first-release foundation and revise parts of ADR-002 and ADR-004; those sections are annotated. ADR-013 adds agent access.

**Revision 2026-10-14:** Email is the first source again (ADR-014, superseding ADR-010). ADR-015 sets the mailbox scope boundary, ADR-016 makes the unsupported-attachment rule in ADR-006 a configured policy, ADR-017 covers classification and the three agent clients, and ADR-018 records the database, language and hosting choices. The repo-root `CLAUDE.md` holds the full build context and wins where it disagrees with these docs.

## ADR-001: One Repository, Common Platform, Separate Adapters

Use one monorepo for email, Teams, Fireflies and Dialpad. Begin with Python for the Azure Functions and shared packages, allowing reuse of the earlier email prototype. Shared packages may not assume every communication has an email mailbox, message subject or recipient list.

Initially implement only Fireflies (ADR-010); email follows, then Teams. Future adapter directories receive coverage/contract documentation when useful; do not build fake clients, enable timers or request broad permissions for future sources. Add each adapter in its own release after discovery and contract tests.

Deploy capture/processing, retrieval and maintenance with boundaries appropriate to their permissions. Do not create a microservice for every processing step. Start with clear module interfaces and split for independent scaling or security needs. Shared packages must be included in each build artifact and versioned with it.

## ADR-002: SQL Is Authoritative; Search Is Rebuildable

Use Azure Database for PostgreSQL for normalized source occurrences, versions, permissions, jobs, checkpoints and audit metadata. Store large originals and extracted artifacts in private Blob Storage. Azure AI Search stores derived keyword/vector evidence.

> **Revised by ADR-012:** for the first release, PostgreSQL (pgvector + full-text) *is* the search index, so there is no separate search service. The rule below applies again once Azure AI Search is introduced.

Do not add a separate pgvector search implementation to the first release. Rebuild search from eligible SQL/artifact versions. Publish SQL evidence state only after the relevant search version is complete, and require that state at retrieval. A partially written index must not expose unpublished evidence.

Use the PostgreSQL durable queue initially with transactional capture/checkpoints, leases, retries and dead-letter state. Define a queue interface and outbox so a dedicated broker can be introduced based on measured load. Limit historical backfill separately to preserve capacity for new email.

## ADR-003: An Occurrence Is Distinct From a Conversation

Identify records by environment/dataset, provider, tenant/account, source scope and native ID. Store revisions separately. An email copied across mailboxes has distinct occurrences and may have distinct BCC visibility. A meeting found in Teams and Fireflies has linked source occurrences with independent provenance and permissions.

Use relationships for threads, replies and likely shared meetings. Do not merge source records or access grants on a matching subject, sender, phone number or content hash. Entity associations such as Asset/Project links require evidence and may remain unresolved.

## ADR-004: One Shared Mode Setting

The desired operator control is `INGESTION_MODE=test|production|paused`.

| Mode | Capture and Processing | Search |
|---|---|---|
| `test` | Enabled providers intersect explicit test selections with test-approved registry scopes | Current approved test dataset only |
| `production` | Enabled providers use production-approved registry scopes | Current approved production dataset only |
| `paused` | No new capture, extraction, embeddings or publication | No communication evidence returned |

Mandatory withdrawals, retention and audit maintenance may continue while paused. Emergency infrastructure shutdown is a separate action. Already returned content cannot be recalled.

> **Revised by ADR-011:** for the first release, the active mode and revision live in the PostgreSQL `runtime_config` table rather than App Configuration. The behavior below (one switch, revisions stamped on jobs, fail closed) is unchanged.

For one local process, read environment variables. For deployed apps, propose one Azure App Configuration key for `INGESTION_MODE` and versioned scope settings. The environment file documents names, not an instruction to edit the same variable manually across every app.

A mode controller validates the requested state and activates a SQL configuration revision. Workers carry that revision and dataset on jobs. Before external source reads, publication or retrieval, services validate the active revision. During a transition, block new publication/retrieval until acknowledgement; stale workers abort publication. Reads already in flight may finish into restricted staging but cannot publish under a retired revision. A short configuration lease bounds stale processing; failure to refresh fails closed. Exact lease and transition timeout values require implementation tests.

Changing mode does not approve registry rows, grant provider permissions, promote test content, change backfill dates, alter retention or enable new sources. Future adapters have independent enable flags defaulting to false. Switching back to test is reversible but is not a deletion of production evidence.

Separate deployment environment from mode. Development and production resources/identities remain distinct. A production deployment may run in test mode against a small approved mailbox set before launch. Prepare source permissions and approvals ahead of the switch if a one-setting launch is required.

## ADR-005: Explicit Reader Entitlements

Ingestion credentials are not reader permissions. For the email pilot, propose an explicitly approved executive corpus and separate ingestion and retrieval identities. The business must approve the mailbox inventory and executive audience. Restricted scopes are excluded unless separately authorized and supported.

The API derives access from validated user identity and current grants, never caller-provided principal lists or model instructions. Future Teams/Fireflies/Dialpad evidence must use source ACL mapping or an explicit corpus entitlement; an email grant does not imply access to those sources.

Every summary, chunk, citation and destination projection must satisfy its evidence permissions. Permission changes trigger withdrawal even without a content edit. Direct Copilot access to a broad index must not bypass the API.

## ADR-006: Screening and Extraction Before Publication

Reject known spam/threats and excluded scopes as early as practical. Stage only authorized material needed for processing. Attachments must pass malware checks before extraction. The first release accepts only PDF attachments. Extract native PDF text first where reliable; OCR scanned pages within PDFs with source/page attribution. Standalone images and other files remain unsupported even if the OCR service supports them. Evaluate extracted content for sensitive information and prompt attacks before embedding.

> **Revised by ADR-016:** E1 records attachments as metadata only, and the unsupported-attachment rule below is now the configured `ATTACHMENT_UNSUPPORTED_POLICY`. PDF extraction arrives in E2.

Unknown malware/threat state, unavailable policy services, low-quality extraction and unsupported/encrypted files remain pending or held. The initial conservative rule is whole-message hold when an attachment cannot be evaluated, including unsupported inline images. This coverage consequence and the PDF-only rollout contract are documented in [Email Layers and PDF Scope](EMAIL_PIPELINE_AND_PDF_SCOPE.md). Do not silently index incomplete evidence as complete.

OCR does not resolve access control, encryption, malicious files or sensitivity classification. Detection quality is fallible and requires representative tenant tests. Historical email without usable threat evidence requires a separate validated strategy; a clean test dataset does not establish historical coverage.

## ADR-007: GitHub Owns Deployable Releases

Use GitHub Actions with Azure OIDC, environment-scoped deployment identities and managed runtime identities. Never carry the prototype's personal-repository federation subject into this repo. Intended subjects use `repo:Finishes-Solutions/fs-communication-intelligence:environment:<environment>`.

Keep infrastructure, migrations and application release workflows distinct. Build immutable artifacts; promote the same tested versions. Routine releases preserve the active mode and registry approvals. Capture commit, schema, policy and configuration versions in a release record. Rollback must respect schema compatibility.

PR tests include provider-independent contract tests, database behavior, authorization and synthetic processing cases. Cloud acceptance validates actual permissions, Defender/OCR behavior and Copilot authentication. Bicep compilation alone is not a deployment test.

## ADR-008: Evidence and Business Decisions Remain Distinct

Classification and routing produce proposals with provenance. An email statement, transcript summary or extracted commitment is not automatically a verified decision, payment approval or governed record.

monday is a controlled projection of eligible records with stable mappings, not the primary store for large transcripts. An evidence link preserves the protected reader path. Uploading transcript files or updates requires verifying the board audience and lifecycle behavior first.

## ADR-009: Lifecycle and Operations Are Release Requirements

Deletion/revocation blocks retrieval before physical cleanup. Propagate cleanup to original artifacts, extracted text, chunks, indexes and destination copies subject to explicit retention/hold policy. Track tombstones so restores/replays do not resurrect withdrawn records. Define backup and soft-delete expiry separately.

The first production release requires alerts, queue/reconciliation metrics, dead-letter replay, recovery drills and a measured backfill/cost plan. They are not deferred behind future source integrations.

## ADR-010: Fireflies Is the First Source

> **Superseded by ADR-014 (2026-10-14).** Email is the first source. Fireflies is undecided and gets no runtime code for now. The Fireflies docs remain reference material for the shared foundation.

Decided 2026-09-29 by Matthew: build Fireflies first, then email, then Teams. Fireflies has no attachments, malware or threat gates, or cross-mailbox copies, and its volume is small (about 42 hours of transcripts). It still proves every shared capability: scoped capture, the durable queue, screening, citations, embeddings, authorized retrieval, the mode switch, deletion and agent access. Email and Teams add only their source-specific work. See [Fireflies Pipeline](FIREFLIES_PIPELINE.md) and [Fireflies Backlog](FIREFLIES_RELEASE_BACKLOG.md).

The Fireflies API uses a user-bound bearer key rather than a managed identity. Store it in Key Vault, prefer a dedicated seat, and treat the webhook as an optimization: reconciliation sweeps are the source of truth, including for deletions.

## ADR-011: Simplified First-Release Foundation

Proposed. The earlier foundation (three Function Apps, App Configuration with configuration leases, a VNet from day one) was sized for company-wide email. For the first release:

- **Two Function Apps**, ingestion and search, with separate managed identities and database roles. Maintenance runs as timers in the ingestion app. The maintenance identity stays unused until a separate app is justified.
- **The mode lives in PostgreSQL** (`runtime_config`, one active revision enforced by a unique index; `active_dataset()` drives the retrieval views). Jobs carry the revision they were admitted under, and publication re-checks it. App Configuration is deferred.
- **Networking:** the development stack uses synthetic data with an Entra-only, firewall-restricted database endpoint. The production stack, which runs real meetings even in `test` mode, uses VNet integration and private PostgreSQL access.

Security boundaries are unchanged: separate ingestion and retrieval authority, fail-closed scope, and no content in logs. See [Function Apps](FUNCTION_APPS.md).

## ADR-012: PostgreSQL Search for the First Release

Proposed. Use pgvector (HNSW, cosine, 1536 dimensions) and PostgreSQL full-text search with Reciprocal Rank Fusion, inside the authoritative database. Reasons:

- At pilot volume (about 1–2k chunks) a separate search service adds cost and a consistency problem without a quality benefit that has been shown.
- Publication becomes a single transaction. Authorization, exclusions, tombstones and mode are enforced by views in the same query, so there is no filter-string mirroring into an external index.
- The search role can read only `published_*` views. This was verified locally.

Keep a `SearchBackend` interface. Move to Azure AI Search when a measured trigger occurs (email volume, filtered-vector recall or latency, or semantic-ranker value shown in evaluation). ADR-002's "search is a rebuildable derivative" rule then applies. See [Search and Agents](SEARCH_AND_AGENTS.md).

## ADR-013: Agents Reach Evidence Only Through the Authorized API

> **Revised by ADR-014/017:** the first MCP tools are `search_email`, `get_email` and `list_email`. The authorization rules below are unchanged.

Proposed. The search app exposes the same retrieval core in two ways: REST/OpenAPI, and an MCP endpoint (`search_meetings`, `get_meeting`, `get_transcript_window`, `list_meetings`). Every call carries the user's delegated Entra token. Reader authority comes from app roles in the token mapped to corpora, never from caller parameters. No agent receives a database connection, index key or service credential. Returned text is marked as untrusted evidence, and Fireflies summaries are labelled as AI output. Entra ID has no dynamic client registration, so each agent platform uses a pre-registered client, tested per platform.

## ADR-014: Email Is the First Source

> **Source order amended by ADR-020 (2026-10-28):** email → Fireflies → Teams. Fireflies now has runtime code.

Decided by Matthew, recorded 2026-10-14; supersedes ADR-010. Source order is **email → Teams → (Fireflies, Dialpad: undecided)**. Only email gets runtime code now. Shared packages (`ci_core`, `ci_processing`, `ci_retrieval`) stay source-neutral so Teams can reuse them, but no Teams, Fireflies or Dialpad clients are built.

Email ships in increments:

- **E1:** body text. Attachments are recorded as metadata only (name, type, size, inline flag, content ID).
- **E2:** PDF extraction and OCR behind malware scanning (Defender for Storage), as its own PR series. PDF remains the only attachment format confirmed for extraction. Other formats come later.

Classified actionable mail will later feed monday.com department intake boards (Exhibit A). That routing is out of scope; the classification output and the `outbox` table are kept so it can be added (ADR-008).

## ADR-015: Exchange Application RBAC Is the Mailbox Boundary

Proposed. Matthew's requirement is "all mailboxes, with the ability to keep specific mailboxes out". Two layers enforce it:

- **Hard layer (Exchange Online Application RBAC).** The ingestion identity gets `Application Mail.Read` through `New-ManagementRoleAssignment` with a custom resource scope. Pilot: members of a pilot group. Production: user and shared mailboxes, minus those with `CustomAttribute15 = 'FS-CI-Exclude'`. **Tenant-wide Entra `Mail.Read` (application) is never granted**: grants are additive, so an Entra grant would silently defeat the RBAC scope. Mail access is read-only; no `Mail.ReadWrite` or `Mail.Send` for any service identity.
- **Soft layer (database).** The `runtime_config` mode, per-mailbox `source_scopes` state and `exclusions`. Exclusions always win, and an empty scope fails closed. An operator CLI (`ci scope list | include | exclude | set-mode`) writes every change to `audit_events`.

An excluded mailbox does not exclude that person's mail in other mailboxes, so `exclusions` also supports participant addresses and `@domain` patterns, evaluated per occurrence before content is staged.

The scope is verified with `Test-ServicePrincipalAuthorization` **and** a real runtime token: 200 on an in-scope mailbox and 403 on an excluded one. A 401 or 404 is not a valid negative test. Permission changes can take up to about two hours to apply.

## ADR-016: Unsupported-Attachment Handling Is a Configured Policy

Proposed; the business decision (D-E1) is open. ADR-006's whole-message hold for unsupported attachments would hold most mail, because most messages carry inline signature images. The rule becomes a setting:

`ATTACHMENT_UNSUPPORTED_POLICY = hold_message | publish_partial | exempt_cid_inline`

- `hold_message`: the original conservative rule.
- `publish_partial`: publish the body and report unprocessed attachments as partial coverage.
- `exempt_cid_inline`: ignore inline (`contentId`) images; hold for any other unsupported attachment.

Local and dev default to `publish_partial`. **Production has no default**: the ingestion app refuses to start unless the value is set explicitly. Search results always report held and partial counts in a coverage block; incomplete evidence is never presented as complete.

## ADR-017: Classification Produces Proposals; Three Agent Clients

Proposed.

**Classification.** A `Classifier` interface with `CLASSIFIER_BACKEND = azure_openai | decisions_api | fake`. Defaults: `fake` locally and `azure_openai` in the cloud. `decisions_api` sends email content to OpenAI and stays off until Matthew confirms data-processing approval (D-C1). Output is stored as proposals with model, policy version, input hash, per-label probability and decision, never as approvals. The starting labels and thresholds in `CLAUDE.md` §7 are uncalibrated and must be calibrated on a labelled pilot sample. Items with credentials, potential PHI or sensitive PII are marked restricted (not embedded, never returned). Spam and newsletters are recorded but not indexed.

**Agent clients.** Claude, Microsoft Copilot (Copilot Studio) and ChatGPT, each signing in as the user through a pre-registered Entra client app (Entra has no dynamic client registration). Reader authority comes from app roles (`Communications.Reader.All`, `Communications.Reader.Own`), never from tool arguments. ChatGPT is the highest-risk client: if it rejects a pre-registered client, it is recorded as blocked and a thin OAuth broker is proposed, rather than a silent workaround. Which clients are approved for production is open (D-A1); all three are enabled in dev.

## ADR-018: PostgreSQL, Python and Flex Consumption

Proposed; confirms and extends ADR-011/012.

- **Database:** Azure Database for PostgreSQL Flexible Server, **PostgreSQL 18 with pgvector 0.8.2** (the latest Microsoft lists for PG18), chosen over Azure SQL after a comparison. pgvector 0.8's iterative index scans (`hnsw.iterative_scan = relaxed_order`) keep permission-filtered vector search returning full pages when a user can see only a small share of the corpus. PostgreSQL is also the job queue (`FOR UPDATE SKIP LOCKED`, leases, dead state) and the search index (HNSW plus full-text, fused with RRF). `JobQueue` and `SearchBackend` interfaces keep Service Bus and Azure AI Search available later. The extension version must be checked on the provisioned server.
- **Language and hosting:** **Python 3.14** (GA on Azure Functions; Flex Consumption is the plan that accepts it); Azure Functions v2 programming model on **Flex Consumption**. Dependencies are built in GitHub Actions and deployed pre-built, because a Flex remote build for 3.14 has been reported to fail; fall back to 3.13 if the deploy spike in PR 8 hits a blocker. CI and local development use the same versions (`.python-version`, `pgvector/pgvector:0.8.2-pg18`).
- **Two deployables:** `ingestion` (capture, worker, maintenance timers) and `search` (REST + MCP), with separate managed identities. The search identity never gets mailbox, Blob-write or database-write access. A hosting spike decides whether the MCP endpoint runs inside Functions or as an ASGI app on App Service or Container Apps; its result will be recorded as a new ADR.
- **Embeddings:** Azure OpenAI `text-embedding-3-large` at 1536 dimensions in the Finishes tenant, recorded in the processing profile.

## ADR-019: Hosted MCP in the Search Function App; Scheduled Capture in the Ingestion Function App

Decided 2026-10-28 (hosting spike from CLAUDE.md §8, recorded with tests rather than a live deployment).

- **MCP hosting.** The search Function App (Flex Consumption) serves MCP over Streamable HTTP at `/mcp` through `azure.functions.AsgiFunctionApp`, in stateless mode with JSON responses so no session state or long-lived stream is needed. The Functions ASGI adapter runs the app's startup hook, which the MCP SDK requires; tests drive real requests through that adapter. Functions-level auth is anonymous; the app itself rejects any request without a valid Entra v2 delegated token (signature, issuer, tenant, audience, expiry, `scp` = `Communications.Search`) and publishes OAuth protected-resource metadata naming Entra as the authorization server. Reader authority comes from the token's app roles and object id. App Service or Container Apps remain the fallback if the Functions host proves unsuitable in practice.
- **Clients.** Entra has no dynamic client registration, so Claude, ChatGPT and Copilot Studio each get a pre-registered confidential client. ChatGPT is the highest risk; if it requires dynamic registration, it is recorded as blocked and a thin OAuth broker is proposed, not shipped silently.
- **Capture.** Until per-folder delta and notifications exist (PR 5), the ingestion Function App runs a timer capture over a rolling window (`CAPTURE_HOURS`, default 2, overlapping runs dedupe) and a processing timer with a time budget. Both are off until switched on by app settings, and both obey `runtime_config` mode.
- **Identities.** Mail is read by the ingestion managed identity through Exchange RBAC; the database logins of the two managed identities belong only to `ci_ingest` and `ci_search` respectively.


## ADR-020: Fireflies Meetings Are the Second Source, Without PII Screening

Decided by Matthew 2026-10-28 ("pull Fireflies meetings from the whole company and have AI analysis done, no scanning for PII"); amends ADR-014's source order to **email → Fireflies → Teams**. Details in [Fireflies Pipeline](FIREFLIES_PIPELINE.md).

- **Scope.** One `fireflies_workspace` scope stands for the whole Fireflies team. People are kept out with participant exclusions (checked before storage and again by the published views), single meetings with item exclusions. The API key is user-bound; capture lists the key's default view plus each member by `user_id` and de-duplicates. Whether that reaches every member is unverified until the first real run.
- **No screening.** Meetings skip Azure AI Language PII detection, Prompt Shields and masking. Each version records a `not_screened_by_policy` decision so the gap is visible in the data. Consequence: sensitive conversations (HR, medical, compensation) are indexed and readable by anyone with `Communications.Reader.All` unless their participants are excluded. Transcript text is still treated as untrusted by the analysis prompt.
- **Analysis.** A source-neutral meeting analyzer (summary, decisions, action items, project risks, names, a subset of the §7 labels) produces proposals with model, policy version and input hash, like email classification.
- **Hosting.** New timers in the existing ingestion Function App, with their own kill switch (`FIREFLIES_ENABLED`) and schedule table, instead of a third Function App. This avoids a new identity, database login and deploy pipeline; the cost is a shared failure domain with email capture.
- **Capture is polling only.** No webhook and no deletion detection yet (Fireflies has no deletion webhook; a reconciliation sweep is future work).
- **Retrieval.** Published meeting views exist; meeting MCP/REST tools are a follow-up. Email search filters to email so meeting chunks never displace email results.
