# Email Intelligence Release Backlog

> **2026-10-14:** email is the first release again ([ADR-014](ARCHITECTURE_DECISIONS.md#adr-014-email-is-the-first-source)). Foundation items CI-002 to CI-006 are delivered here, not in the Fireflies backlog. E1 records attachments as metadata only; CI-010/CI-011 (PDF) move to E2. The PR order in `CLAUDE.md` §12 takes precedence over the order below.

Status: planned, not started. **Formerly the second release:** Fireflies ships first ([ADR-010](ARCHITECTURE_DECISIONS.md#adr-010-fireflies-is-the-first-source)); foundation items CI-002 to CI-006 are delivered as FF-003 to FF-007 in the [Fireflies Release Backlog](FIREFLIES_RELEASE_BACKLOG.md), and CI-014/CI-015 build on its PostgreSQL search and API. Repository access and initialization are complete. IDs below are planning references, not GitHub issues already created. Suggested responsibility is a role, not a staff assignment or deadline.

The first release is a working, mailbox-scoped Email Intelligence Tool with authorized executive search. Teams, Fireflies and Dialpad runtime integrations are excluded. Their common record/adapter requirements shape the foundation.

## A. Foundation

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| CI-001 | Confirm email coverage, reader corpus, restricted exclusions, backfill boundary and PDF-only file policy | Business/security decisions | Versioned coverage matrix identifies user/shared mailboxes, ordinary folders, archive exclusions, known gaps and approved audience |
| CI-002 | Common communication/event/artifact contracts and connector interface | CI-001 | Versioned schemas distinguish provider/account/scope, source revision, operation and permissions; synthetic future-source examples validate without email-only required fields |
| CI-003 | Monorepo Python packaging and CI | CI-002 | Shared packages are present in each built artifact; contract/unit/DB checks run in GitHub; no deployment on a planning-only change |
| CI-004 | Azure infrastructure, identities, Key Vault and configuration | CI-003 | Development stack provisions through GitHub OIDC; ingestion/retrieval permissions are separate; resource inventory and cloud costs reviewed |
| CI-005 | SQL migrations, queue, checkpoint and outbox foundations | CI-002, CI-004 | Versioned migrations apply safely; job/cursor transaction, duplicate admission, leases, crash recovery and dead-letter tests pass |
| CI-006 | Shared mode/scope enforcement | CI-004, CI-005 | Test/production/paused transition is coordinated across apps; invalid/empty scope fails closed; stale jobs cannot publish into another dataset |

Suggested responsibility: Technology owns implementation; tenant administrators own source and Azure consent; designated business/security owners approve corpus and policy.

## B. Email Ingestion and Safe Processing

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| CI-007 | Mailbox inventory and scoped Graph access | CI-001, CI-004, CI-006 | Approved user/shared mailboxes enumerate correctly; negative permission test proves an excluded mailbox is inaccessible |
| CI-008 | Folder delta capture and reconciliation | CI-005, CI-007 | Initial/delta paging, immutable IDs, moves, edits, removals, cursor expiry and throttling are replayable with no checkpoint loss |
| CI-009 | Threat/spam policy and historical coverage strategy | CI-001, CI-007 | Actual tenant verdicts validated; known threats excluded; unknown/old verdicts held; decision recorded for historical email coverage |
| CI-010 | Private originals and malware-gated attachments | CI-004, CI-008, CI-009 | Hash-addressed/versioned evidence is protected; only verified clean files reach parsers; scan delay/failure/cap conditions do not pass |
| CI-023 | Cross-mailbox email/artifact deduplication and action idempotency | CI-005, CI-008, CI-010 | Identical eligible content shares durable processing results; simultaneous mailbox copies cannot create duplicate results/actions; divergent copies, revised files and permissions remain distinct; see [deduplication design](EMAIL_DEDUPLICATION.md) |
| CI-011 | PDF-only extraction and OCR | CI-010, CI-023 | Text/scanned/mixed PDFs preserve page provenance without duplication; standalone images and other formats never enter extraction; type mismatch, embedded payload, unsupported/encrypted/corrupt/oversized cases have explicit held states |
| CI-012 | Sensitivity and prompt screening | CI-001, CI-011 | Representative policy samples and dependency failures are handled; restricted text does not reach embeddings or destination projections |
| CI-013 | Normalization, classification and evidence links | CI-002, CI-012 | Participants, UTC dates, source links and revisions are correct; AI/rules outputs retain provenance and do not become approvals |

Matthew confirmed PDF-only attachment processing for the first release. Email body text remains in scope; scanned PDFs use OCR. DOCX/XLSX, standalone images, archives and all other formats roll out later. The existing whole-message hold policy includes unsupported inline image attachments; measure and report that coverage gap. See [Email Layers and PDF Scope](EMAIL_PIPELINE_AND_PDF_SCOPE.md).

## C. Search and Executive Pilot

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| CI-014 | Chunking, embeddings and hybrid index | CI-013 | Citation-preserving chunks are published by version; partial uploads remain inaccessible; reindex is idempotent and model versions are tracked |
| CI-015 | Authorized retrieval API | CI-006, CI-014 | Valid delegated users receive only current authorized evidence; wrong identity, revoked access, stale source state and caller-forged filters fail |
| CI-016 | Executive Copilot agent | CI-015 | Each executive authenticates as themselves; answers cite evidence and indicate incomplete coverage; text from sources is treated as untrusted evidence |
| CI-017 | Pilot evaluation | CI-008 through CI-016, CI-023 | Synthetic and selected live mailbox acceptance passes; held-content and no-result behavior is understandable; relevance evaluated against expected evidence |

Optional email-launch integration: CI-018, controlled monday projection. Confirm board capabilities and audience; map internal IDs to stable item IDs; sync approved summaries/links and withdrawals through the outbox. If projection is not part of the email launch, keep it disabled and scheduled explicitly. Existing Dialpad zaps are not altered by this email release.

## D. Production Requirements

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| CI-019 | Retention, deletion and permission reconciliation | CI-005, CI-015 | Withdrawals block results first; artifacts/chunks/index/destination copies follow policy; restore and replay do not resurrect tombstoned evidence |
| CI-020 | Monitoring, alerts and support runbooks | CI-008 through CI-017 | Capture lag, oldest job, scan wait, held/dead counts, scope changes, search failures and cost are visible; alert delivery and ownership tested |
| CI-021 | Capacity, cost and recovery qualification | CI-017, CI-019, CI-020 | Measured representative workload supports agreed freshness/backfill targets; restore and release rollback drilled; estimated monthly/backfill cost accepted |
| CI-022 | Production release and phased mailbox expansion | CI-009, CI-017, CI-019, CI-020, CI-021 | Production inventory and permissions approved; tested artifact promoted; one mode switch activates only approved email scope; counts and alerts reconcile |

Do not equate CI-017 pilot acceptance with CI-022 production readiness. Retention and historical threat coverage are required decisions before a claim of full-history coverage. Limited recent-mail production coverage may be accepted explicitly while older mail remains held; communicate that boundary to the agent and users.

## E. Future Source Work

After email launch, choose Teams, Fireflies or Dialpad by business priority. For each, complete API entitlement/coverage discovery, identity/ACL mapping, webhook/polling replay design, edits/deletions, source-specific normalization and the same authorization/lifecycle acceptance. Turn on only that adapter after its own test scope passes.

## First Implementation PRs

1. **Contracts and package scaffold:** CI-002/003 with synthetic fixtures; no live cloud resources.
2. **Infrastructure and migrations:** CI-004/005; development environment only.
3. **Scope and capture:** CI-006/007/008 with explicit test mailboxes.
4. **Screening and extraction:** CI-009 through CI-013 plus CI-023 deduplication, with tenant security and concurrent-copy tests.
5. **Search and agent:** CI-014 through CI-017.
6. **Lifecycle and operations:** CI-019 through CI-021, developed alongside the pilot and complete before CI-022.

No dates or staff assignments are asserted. Scope, service availability, representative workload and acceptance ownership are needed before estimating a delivery schedule.
