# Communication Intelligence Platform Plan

Date: September 25, 2026. Status: proposed architecture and implementation sequence; planning PR, not deployed software.

Authoritative repository: https://github.com/Finishes-Solutions/fs-communication-intelligence

This repository will own email, Teams, Fireflies and Dialpad ingestion. Email is the first release. The other sources receive extension points now, not active integrations. Access was verified on September 25, 2026. The repository was empty, with no existing branch, code, instructions or workflows. A minimal README initialized main; this plan is proposed in a separate planning branch.

## 1. Product Boundary

Build one communication platform with independent source adapters and shared evidence processing. Executives search authorized evidence through an authenticated Copilot agent API. monday.com receives a controlled operational projection when that integration is enabled. A statement in a message, transcript or AI summary is evidence, not an approved business decision.

| Component | Responsibility |
|---|---|
| Azure Database for PostgreSQL | Authoritative normalized records, source identities, permissions, processing state, relationships, review/routing state and audit history |
| Azure Blob Storage | Restricted originals, attachments, recordings if approved, extracted text and transcript artifacts; separate quarantine and approved areas |
| Azure AI Search | Rebuildable keyword/vector indexes containing approved chunks and security metadata |
| Azure App Configuration | Shared runtime mode and versioned configuration for the deployed apps |
| Azure Functions | Source capture, durable processing, lifecycle maintenance and the authenticated retrieval API |
| Azure AI services | OCR, sensitivity/prompt screening and embeddings, invoked under the approved processing policy |
| Copilot Studio agent | User-facing evidence retrieval, synthesis and citations |
| monday.com | Communication inventory, processing exceptions and workflow summaries; full evidence remains behind authenticated access |

Recommendation: PostgreSQL plus Azure AI Search. Keep operational state in SQL and make the search index reproducible. Do not maintain a second pgvector index initially. Use a durable PostgreSQL queue for the first measured pilot; isolate the queue interface so a dedicated broker can be introduced when actual throughput warrants it.

## 2. Source Scope

| Source | Record types to support | Initial status | Later implementation must establish |
|---|---|---|---|
| Microsoft 365 email | Individual email messages; attachments as children; mailbox copies separately identified | Build now | Mailbox inventory, shared-mailbox scope, ordinary-folder coverage, historical threat coverage and archive exclusions |
| Teams | Chat/channel messages, replies and meeting evidence where available | Disabled | API permissions, available history, membership changes, private/shared channels, edits/deletions and transcript availability |
| Fireflies | Meetings, transcripts, speakers, timestamps and source summaries | Disabled | Account/API entitlement, history, transcript completion/revision, sharing permissions, deletion and webhook/polling behavior |
| Dialpad | Calls, SMS/MMS where available, transcripts, recordings and participants | Disabled | Account/API entitlement, department/user scope, call legs/transfers, delayed transcripts, media availability and event replay |

These future-source rows describe intended coverage, not verified API capabilities or entitlements. Complete an API and permission discovery task before scheduling each source build. After email acceptance, select the next source by business priority; no dependency requires Teams to precede Dialpad or Fireflies.

## 3. Proposed Repository Structure

Use a monorepo with Python shared packages and independently deployable apps. This is the target layout for the empty repository; directories are added when implementation begins, rather than represented as working services now.

| Path | Purpose |
|---|---|
| `apps/email_ingestion/` | Email timers, Graph capture and source-specific deployment entry point |
| `apps/processing/` | Shared durable worker, extraction, screening and index publication |
| `apps/search_api/` | Delegated identity validation, authorization, retrieval and citations |
| `apps/maintenance/` | Retention, deletions, permission refresh, reconciliation and controlled reindexing |
| `packages/core/` | Versioned contracts, identifiers, configuration, job interfaces and common errors |
| `packages/connectors/email/` | Graph implementation and email normalization |
| `packages/connectors/teams/`, `fireflies/`, `dialpad/` | Connector contracts/coverage notes only until their release is authorized |
| `packages/processing/` | Extraction/OCR, policy, classification, chunking and embedding logic |
| `packages/storage/` | PostgreSQL, Blob, search and queue repositories |
| `packages/authorization/` | Entitlements, source ACL mapping, sensitivity policy and retrieval enforcement |
| `packages/integrations/monday/`, `copilot/` | Destination projection and agent contract, separate from capture |
| `contracts/` | Versioned event schemas and search OpenAPI specification |
| `db/migrations/` | Ordered, reviewed schema migrations |
| `infra/` | Bicep modules and environment parameters without secrets |
| `tests/` | Unit, connector-contract, database, authorization and acceptance tests; synthetic fixtures |
| `docs/` | Architecture, decisions, source coverage, data dictionary, runbooks and rollout plan |
| `.github/workflows/` | CI, infrastructure plan/deploy, app release and migration workflows |

Initially these apps can share a release version. Preserve separate ingestion and retrieval runtime identities so a search caller cannot obtain source ingestion permissions. Build each deployable artifact with its required shared packages included; a sibling directory existing in the repository does not make it available inside a deployed Function App.

## 4. Common Data Model

Normalize around communications and evidence. Keep provider-specific attributes in typed extension records or versioned JSON; do not add email-only mandatory columns to every source.

| Entity | Minimum purpose and fields |
|---|---|
| `source_connections` | Provider, tenant/account, enabled state and credential reference; no stored plaintext credential |
| `source_scopes` | Mailbox/chat/team/department or other provider scope; test/production approval, exclusion, checkpoint and policy version |
| `communications` | Internal ID, provider, kind, source scope/native ID, source version, occurred/modified/ingested times, title, lifecycle state and source URL |
| `communication_versions` | Content hash, source revision, extraction/policy/model versions and publication state |
| `participants` | Provider identity, optional Entra identity, role, display name and normalized address/phone; unresolved identities retained |
| `communication_participants` | Sender/recipient/BCC/attendee/speaker/caller/callee links with field-level visibility where needed |
| `artifacts` | Message body, attachment, transcript, recording or provider summary; storage key, MIME type, hash, scan/extraction status and parent ID |
| `transcript_segments` | Speaker reference, start/end time, text, transcript version and provenance; nullable time for untimed text |
| `chunks` | Communication/artifact/version, text reference, page or time range, hash, embedding model/version and index publication state |
| `access_grants` | Approved source or corpus entitlement, principals/groups, sensitivity boundary, effective dates and revocation state |
| `relationships` | Thread/reply, same-meeting, related-call, project/asset links and proposed duplicate relationships with provenance |
| `jobs`, `checkpoints`, `outbox` | Durable execution, retries, transactional capture and reliable downstream publication |
| `policy_decisions`, `audit_events` | Content-free reason codes, policy version, actor, decision time and action |
| `routing_candidates`, `destination_mappings` | Proposed classifications and governed monday item mappings/revisions |

Identity rule: unique within environment/dataset + provider + tenant/account + source scope + native item ID. Use the provider's durable identifier where supported. Content hashes help detect identical artifacts; they do not establish equivalent permissions.

Keep separate source occurrences for email copies and for a meeting found in both Teams and Fireflies. Link related evidence and collapse authorized duplicate results in the presentation layer. Never union permissions merely because text or meetings appear to match. Repeated SMS messages and call transfers also require native identities, not text-only deduplication.

## 5. Shared Processing Contract

Every connector produces a versioned envelope containing event ID, provider/account/scope, native item ID, operation (upsert/delete/access-change), source version, observed time, dataset, artifact references, correlation ID and a permission snapshot/reference. Missing permissions mean restricted/unpublished, not organization-wide access.

Use the following sequence:

1. Validate connector authentication, configured source enablement and approved scope before reading content. Admit a minimal event to the durable queue.
2. Fetch the current source version and permissions; reject excluded folders/scopes and known threats/spam. A deletion or revocation takes the withdrawal path immediately.
3. Stage required originals privately under quarantine controls. Record hashes and provenance; never put message bodies in queue payloads or operational logs.
4. Admit only PDF file attachments in the first release; scan before parsing. Extract native text or OCR scanned pages inside PDFs. Standalone images and all other formats are unsupported. Preserve whole-message holds for unevaluated attachments, including inline images; see [PDF scope](EMAIL_PIPELINE_AND_PDF_SCOPE.md). No automatic conversion, remote-link fetching or archive expansion.
5. Apply sensitivity and prompt-attack checks to message content and every extracted artifact before embedding or publication. A failed dependency is pending/held, never an automatic pass. OCR is extraction, not a malware or PHI filter.
6. Normalize, classify and associate candidate business entities. Store AI outputs separately from source evidence, with confidence, policy/model version and evidence references.
7. Chunk by useful boundaries: email body/attachment, Teams message/reply context, transcript speaker/time and document page. Preserve citations. Embed only eligible content.
8. Write a new search version, verify publication, then atomically activate its SQL evidence version. Retrieval must require the active SQL version as well as an authorized search hit.
9. Publish optional destination events through the outbox. A monday outage must not block source capture or cause duplicate source records.
10. Reconcile changes and periodically purge expired/deleted derivatives. A retry must resume safely without duplicate chunks, records or board items.

Keep lifecycle, extraction, policy and indexing statuses separate. A message can be captured successfully but held from search. Distinguish pending/retryable failures, policy holds, unsupported content, permanent failures, deleted and published states in operational reporting.

## 6. Access and Data Lifecycle

Separate ingestion authority from reader authority. Executive status alone does not confer access to every source. For the first email release, use an explicitly approved executive corpus, with permitted mailbox inventory and excluded restricted scopes. Future sources must map their own permissions or receive an explicit corpus grant. An email corpus grant must never silently include all private Teams chats.

Enforce authorization before returning chunks to the model, and recheck current publication/revocation state. Every derived summary or chunk must remain within the visibility of its supporting evidence. Support source permission changes independently of content changes. Do not expose a broad Search API key or a direct index connection that bypasses these checks.

Before production, implement a lifecycle worker for source deletions, entitlement revocations, expiry and approved holds. Withdrawal blocks retrieval first; physical removal covers search vectors, chunks, extracted text, original artifacts, caches and monday projections according to policy. Retained audit records contain only necessary metadata. Document backup/soft-delete expiry separately: deleting live evidence is not a claim that backup bytes disappear instantly.

Define different retention rules for quarantined threats, rejected sensitive content, ordinary communication evidence and audit records. Confirm whether restricted raw material may be stored or processed by each AI service; rejecting material from search does not undo its prior processing.

## 7. Test Mode and Release Configuration

Keep the operational switch requested by Matthew:

```dotenv
INGESTION_MODE=test
TEST_MAILBOX_IDS=<approved-mailbox-object-id>,<approved-mailbox-object-id>
SOURCE_EMAIL_ENABLED=true
SOURCE_TEAMS_ENABLED=false
SOURCE_FIREFLIES_ENABLED=false
SOURCE_DIALPAD_ENABLED=false
```

`test` requires both explicit test IDs and test approval in the source registry. Empty or malformed scope fails closed. `production` selects only production-approved scopes for enabled connectors. `paused` stops new source work and content retrieval; mandatory withdrawal/retention maintenance can continue under a separate documented control.

Switching to `INGESTION_MODE=production` must not enable future connectors, discover every tenant account, grant source permissions, skip screening, promote test records or change historical backfill dates. Preconfigure production permissions and registry approvals so this remains one operational setting at launch.

Deployment environment and ingestion mode are different. Use an isolated development stack for code/contract testing and a production stack that can initially run in test mode against a few approved mailboxes. Keep dataset keys, checkpoints, blobs, index namespaces and monday destination bindings separate. For the planned split-app deployment, use one shared Azure App Configuration setting named `INGESTION_MODE`, with versioned mode/scope configuration and a SQL publication fence. The environment-variable form remains the local-development interface. All apps must enforce the same active configuration revision before source reads, publication and retrieval; an expired configuration lease or a partial transition fails closed. The operator changes one shared setting. Implementing and testing this coordinated transition is part of the foundation milestone, not a property of editing several independent Function App variables.

The earlier single-app pilot already implements basic mode selection. This shared configuration contract is a required addition when separating apps.

## 8. GitHub Deployment Plan

1. Access and empty-repository inspection are complete. Review the planning branch, then implement the agreed foundations in small PRs. Selectively reuse the earlier prototype after adapting its contracts and tests.
2. Establish GitHub environments for development and production, scoped Azure OIDC federation, runtime identities and Key Vault references. Federation must name `Finishes-Solutions/fs-communication-intelligence`; the earlier pilot's personal-repository subject is incorrect for this destination.
3. PR checks validate contracts, unit/database tests, authorization cases, dependency/security checks and Bicep compilation. Infrastructure compilation is not a live deployment test.
4. Build versioned deployable artifacts that include shared packages. Promote the same tested artifacts across environments and record commit, schema, configuration and policy versions.
5. Deploy infrastructure through a separate reviewed workflow. Routine code deployment must preserve current ingestion mode and approved registry scope.
6. Apply backward-compatible database migrations with a dedicated migration identity and version ledger. Avoid leaving a database-admin bootstrap password in the normal runtime.
7. Release development automatically after validated merges if desired; use an explicit production release and environment controls. A merge must not silently expand ingestion.
8. Run synthetic smoke tests and health checks; retain a known-good application artifact for rollback. Schema rollback requires its own tested plan; app rollback alone cannot reverse destructive migrations.

## 9. Email-First Delivery Backlog

| Milestone | Work | Required exit evidence |
|---|---|---|
| A. Repository and platform contracts | Access verified; empty repository initialized; adopt architecture decisions, source envelope, common schema and release structure | Reviewed target-repo PR; email names removed from shared interfaces |
| B. Platform foundation | Provision development resources, identities, migrations, durable queue, private evidence storage, audit and test-scope controls | GitHub deployment succeeds; out-of-scope reads fail; empty scope fails closed |
| C. Email capture | Approved mailbox inventory; Graph immutable IDs and folder delta; bounded backfill, moves/deletes, recovery and reconciliation | Repeatable import with no lost checkpoint work or duplicate occurrences |
| D. Safe extraction | Defender evidence, attachment scan, native extraction/OCR, PHI/sensitivity/prompt checks and held-item review | Representative clean content publishes; unsafe/unknown/unsupported cases remain explicit and unsearchable |
| E. Retrieval and executive pilot | Shared chunks, hybrid index, delegated API, entitlement enforcement, Copilot citations and partial-coverage reporting | Approved executive queries pass; unauthorized/revoked/stale evidence cannot be returned |
| F. Production readiness | Physical lifecycle cleanup, monitoring/alerts, restoration drill, realistic throughput/cost test, historical threat strategy and operational runbooks | Written acceptance for coverage, security, retention, capacity and support ownership |
| G. Email launch | Register production mailboxes, preserve near-real-time capacity during staged backfill, set mode to production | Reconciled counts, bounded backlog, alerting and retrieval acceptance across approved corpus |
| H. Additional sources | Select next adapter; verify entitlement/API coverage; repeat connector and permission acceptance | New source works without rewriting shared storage/search; other adapters remain disabled |

Repository inspection is complete. Estimate implementation dates after source API access, security-service availability and tenant prerequisites are confirmed. The previous pilot worker's throughput is not a commitment for company-wide historical ingestion. Measure fetch, scan, OCR, embedding and publication rates separately, and report source inventory counts against captured, held, deleted and published counts. Track cost per message/page/minute as applicable.

## 10. Required Email Acceptance Cases

- Selected user and shared mailboxes; sent and received mail; folders and nested folders; excluded Junk/Deleted/Drafts/Outbox.
- Empty/malformed scope, unapproved mailbox, invalid mode and production-to-test switching without cross-dataset retrieval.
- Repeat events, partial worker failure, source edits/deletions/moves, expired cursors, throttling and retry/dead-letter replay.
- Native, scanned and mixed PDFs; blank/low-confidence OCR; large/encrypted/corrupt PDFs; rejected standalone images, Office files, archives and mislabeled file types.
- Spam/threat evidence, absent/expired threat verdict, attachment scan delay/failure, sensitive content and prompt injection.
- Revoked reader/source grants, wrong-tenant/audience tokens, forged caller filters, stale chunks and partial index writes.
- Thread/BCC boundaries, distinct mailbox copies, UTC timestamps and correct source/page citations.
- Physical deletion and retention propagation, backup restoration with tombstone/revocation reconciliation, realistic backlog and cost measurements.

Automated mocks exercise behavior, not Azure licensing, actual scan signals, live OCR quality or real Copilot authentication. Verify those in the tenant before launch.

## 11. monday Projection and Transcript Handling

The Communication Intelligence board can provide a single normalized operational view for approved records across sources. Suggested shared fields: Communication ID, Source, Type, Occurred At, Title, Participants, Owner/Department, Related Asset/Project, Processing Status, Classification, Summary, Evidence Link and Last Synced At. Confirm dataset-board column/API support and permissions against the real board before implementation; this plan does not assume the earlier proposed capacity or character limits are verified.

Use protected evidence links for full email/transcript content by default. A monday file/doc can be an optional derivative only when the destination audience and retention policy permit it. Avoid making item updates the authoritative transcript store. Keep long transcript segments, speaker/time references and revision history in the evidence layer. A shared board may have broader access than the original source, so titles, participants and summaries require authorization review too.

Upsert by internal Communication ID and persist the monday item mapping. Propagate approved changes, withdrawals and retention actions; retries must not make duplicate items. Governance candidates remain proposals until the designated human/process approves them.

## 12. Treatment of the Earlier Pilot

Earlier work resides in `mattsinitiere/fs-outlook-email-ingestion-tool`, branch `phase3/scoped-email-intelligence`, commit `44207e127f1aa9bc50b778d4dd33f63a9a1418a3`. It was not deployed. Its 28 automated tests and Bicep compilation passed there; these results do not validate a future implementation in the organization repository or the new architecture.

Reuse after review: Graph capture, scope checks, database queue tests, extraction adapters, screening, hybrid retrieval and selected infrastructure modules. Refactor before adoption: email-specific data contracts, mailbox-only access filters, single-app settings, bootstrap, deployment identity subject, source enablement and repository paths. Implement missing production lifecycle and capacity work instead of moving those gaps into an undefined future phase.

The old draft PR was closed as superseded on September 25, 2026. Its branch is preserved as migration input. No prototype code has been imported by this planning change.

## 13. Setup Decisions

GitHub access is resolved. Azure resource configuration, source permissions and live tenant acceptance remain future implementation steps. No secrets should be supplied in chat.

During implementation setup, confirm: pilot mailbox IDs; approved executive corpus and exclusions; backfill boundary; security-service availability; PDF validation and unsupported-attachment policy (PDF-only scope is confirmed); retention rules; production scale/cost budget; and whether monday projection is required for email launch. These do not block adopting the shared-platform plan. Future-source ordering can remain undecided until email passes acceptance.

## Reference Documentation

Design choices above are recommendations; source capability discovery remains a build gate.

- Graph immutable identifiers: https://learn.microsoft.com/en-us/graph/outlook-immutable-id
- Per-folder message delta: https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0
- Graph change tracking: https://learn.microsoft.com/en-us/graph/delta-query-overview
- Azure retrieval architecture and access controls: https://learn.microsoft.com/en-us/azure/foundry/concepts/retrieval-augmented-generation

