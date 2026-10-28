# Fireflies Release Backlog (First Release)

> **Superseded.** Email became the first source on 2026-10-14 (ADR-014), and Fireflies capture was built as the second source on 2026-10-28 ([ADR-020](ARCHITECTURE_DECISIONS.md#adr-020-fireflies-meetings-are-the-second-source-without-pii-screening)), without PII screening and without webhooks. The current state is in [Fireflies Pipeline](FIREFLIES_PIPELINE.md). Keep this as historical reference.

Status: planned, not started. IDs are planning references, not GitHub issues. No dates or staff assignments are asserted.

The first release is **authorized semantic search over Fireflies meetings, available to executives through an API and MCP agents**. It also builds the shared foundation that email (second) and Teams (third) reuse. Email-specific work stays in the [Email Release Backlog](EMAIL_RELEASE_BACKLOG.md); its foundation items CI-002 through CI-006 are delivered here as FF-002 through FF-006.

## A. Discovery and Decisions

| ID | Deliverable | Done When |
|---|---|---|
| FF-001 | Fireflies API discovery | Plan tier and real rate limit recorded. Confirmed whether an admin key lists and fetches other users' transcripts. Transcript schema fields captured from a live call into a **synthetic** fixture. Webhook V2 availability, signature and team-wide vs per-user configuration confirmed. Paging behaviour at 50 results tested. History volume counted. Zapier integration inventoried |
| FF-002 | Decisions FF-D1 to FF-D9 | Key owner, reader audience, restricted-meeting rule, PII hold categories, media storage, summary indexing, pilot scope and backfill boundary, Zapier stance, query-text logging, and retention periods (FF-D8), all recorded in [the pipeline doc](FIREFLIES_PIPELINE.md#5-decisions-needed) |

## B. Foundation (shared with every later source)

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| FF-003 | Repo scaffold: `apps/ingestion`, `apps/search`, `packages/ci_*`, Python 3.11 tooling, and CI (lint, unit, SQL checks in a PostgreSQL+pgvector service container) | — | CI runs on PRs; each app builds an artifact containing its packages |
| FF-004 | Contracts: source event envelope, communication/version/segment/chunk models, connector interface | FF-003 | A synthetic Fireflies fixture and a synthetic email fixture both validate without email-only required fields |
| FF-005 | Database: finalize and apply `0001_foundation.sql` via a migration job; role principals for both managed identities | FF-003 | Migration applies in development; the checks in `db/tests` pass in CI and against the dev server |
| FF-006 | Mode and scope enforcement (`runtime_config`, `admin_mode`, fail-closed checks in every function) | FF-005 | Empty scope, paused mode, a disabled provider and stale revisions all fail closed in tests |
| FF-007 | Infrastructure (Bicep) for development: two Flex Consumption apps, storage, PostgreSQL, Key Vault, Azure OpenAI embedding deployment, AI services, monitoring; GitHub OIDC workflows | FF-003 | Development deploys from GitHub; role assignments match [Function Apps §5](FUNCTION_APPS.md#5-identities-and-permissions); costs reviewed |

## C. Fireflies Capture and Processing

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| FF-008 | Fireflies client: GraphQL, windowed ID-deduped paging, rate budget, 429 handling | FF-001, FF-005 | Contract tests with recorded synthetic responses; a full page splits the window |
| FF-009 | `ff_webhook` with HMAC verification (V1 and V2) | FF-006 | Bad signatures rejected; duplicates collapse to one job; no body logging |
| FF-010 | `worker` + `ff_fetch` + raw staging + normalization into meetings, participants and segments | FF-008 | Replay of the same meeting creates no new version; edits create one |
| FF-011 | `ff_reconcile` + `ff_backfill` + deletion detection and `withdraw` | FF-010 | A missed webhook is recovered; a deleted meeting is hidden then purged; backfill never starves live capture |
| FF-012 | Screening: PII and Prompt Shields adapters, exclusions, title hold rules | FF-002, FF-010 | Samples hold with reason codes; a service outage stays pending |
| FF-013 | Chunker, contextual embeddings, atomic publication | FF-012 | Chunks cite segment and time ranges; partial writes invisible; profile change re-embeds |

## D. Search and Agents

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| FF-014 | Search API app registration + app role; token validation; `search`, `get_meeting`, `get_transcript_window` | FF-013 | Authorized users get cited results; wrong audience, missing role, forged corpus, excluded and paused cases all return nothing |
| FF-015 | MCP endpoint + OAuth protected-resource metadata; tested with Claude and Copilot Studio | FF-014 | Each client signs in as the user and calls every tool; tool descriptions mark content as untrusted |
| FF-016 | Retrieval evaluation (question set, metrics, chunk-size and fusion tuning) | FF-014 | Recall@10 and MRR recorded; the chosen settings are justified by results |

## E. Production Readiness and Launch

| ID | Deliverable | Depends On | Done When |
|---|---|---|---|
| FF-017 | Production stack with VNet/private PostgreSQL, alerts, dashboards, runbooks | FF-007 | Alerts reach an owner; restore drill done; no public database endpoint |
| FF-018 | Retention and purge verified end to end | FF-011 | Superseded, withdrawn and excluded data is purged on schedule; tombstones prevent resurrection after restore |
| FF-019 | Pilot in `test` mode on real meetings (FF-D6 scope), then production approval of scopes and mode switch | FF-015 to FF-018 | Counts reconcile (Fireflies list vs captured vs held vs published); executives accept answer quality; release record written |

## Suggested PR Sequence

1. FF-003 + FF-004: scaffold, contracts, CI (synthetic only).
2. FF-005 + FF-006: migration runner, roles, mode controller.
3. FF-007: Bicep and deployment workflows (development).
4. FF-008 to FF-011: Fireflies capture.
5. FF-012 + FF-013: screening, chunking, embeddings, publication.
6. FF-014 to FF-016: search API, MCP, evaluation.
7. FF-017 to FF-019: production readiness and launch.

FF-001 and FF-002 can start immediately and don't block PRs 1–3.

## After Fireflies

- **Email:** CI-007 to CI-023 from the [Email Release Backlog](EMAIL_RELEASE_BACKLOG.md), on this foundation. It adds Graph capture, threat and malware gates, PDF extraction and OCR, cross-mailbox deduplication, and email chunk kinds to the same search API. Azure AI Search is reconsidered then ([ADR-012](ARCHITECTURE_DECISIONS.md#adr-012-postgresql-search-for-the-first-release)).
- **Teams:** chats, channels and meetings. Discovery must settle per-conversation ACL mapping and how Teams meetings link to Fireflies meetings (`same_meeting` relationships).
