# Proposed Architecture Decisions

Status: proposed for the first implementation release. These describe intended behavior, not features already implemented.

**Revision 2026-09-29:** Fireflies is now the first source (ADR-010). ADR-011 and ADR-012 simplify the first-release foundation and revise parts of ADR-002 and ADR-004; those sections are annotated. ADR-013 adds agent access.

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

Proposed. The search app exposes the same retrieval core in two ways: REST/OpenAPI, and an MCP endpoint (`search_meetings`, `get_meeting`, `get_transcript_window`, `list_meetings`). Every call carries the user's delegated Entra token. Reader authority comes from app roles in the token mapped to corpora, never from caller parameters. No agent receives a database connection, index key or service credential. Returned text is marked as untrusted evidence, and Fireflies summaries are labelled as AI output. Entra ID has no dynamic client registration, so each agent platform uses a pre-registered client, tested per platform.
