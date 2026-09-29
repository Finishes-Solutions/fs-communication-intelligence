# Function Apps

Status: proposed design. No Function App, infrastructure template or workflow exists yet. This replaces the three-app layout in the platform plan for the first release with **two apps** (see [ADR-011](ARCHITECTURE_DECISIONS.md#adr-011-simplified-first-release-foundation)).

Related: [Fireflies Pipeline](FIREFLIES_PIPELINE.md) · [Database](DATABASE.md) · [Search and Agents](SEARCH_AND_AGENTS.md)

## 1. Two Apps, Two Identities

| App | Name pattern | Runtime identity | Purpose |
|---|---|---|---|
| Ingestion | `func-fs-ci-ingest-<env>-<suffix>` | `id-fs-ci-ingestion-<env>` | Webhooks, capture timers, the job worker, screening, embedding, publication and maintenance |
| Search | `func-fs-ci-search-<env>-<suffix>` | `id-fs-ci-search-<env>` | Authenticated REST and MCP retrieval for people and agents |

Why two apps and not one: the search app faces users and agents, so it must not hold the Fireflies API key, Blob write access or database write access. Why not three: maintenance jobs (purges, lease recovery) need roughly the same permissions as ingestion, and a third app adds deployment and cost without a real security boundary at pilot scale. The bootstrap's `id-fs-ci-maintenance-<env>` identity is left unused until a separate maintenance app is justified.

**Hosting:** Azure Functions **Flex Consumption**, Python 3.11, v2 programming model (decorators in `function_app.py`). Flex Consumption supports managed identity, VNet integration and per-function scaling. The search app should have one always-ready instance so agents don't hit cold starts; the ingestion app can scale to zero. Confirm region availability and the supported Python versions at build time.

## 2. Ingestion App Functions

| Function | Trigger | Does | Must never |
|---|---|---|---|
| `ff_webhook` | HTTP `POST /api/fireflies/webhook` (function key in URL **and** HMAC) | Verify `x-hub-signature` on the raw body in constant time; parse V1/V2; admit a `ff_fetch` job; write a `webhook_receipts` row; return 200 | Fetch transcripts, decide scope or log the body |
| `ff_reconcile` | Timer, every 30 min | For each approved scope: list a rolling window, enqueue new or changed IDs. Nightly: sweep IDs back to `backfill_since` and enqueue `withdraw` jobs for IDs missing twice | Run while mode is `paused` or Fireflies is disabled |
| `ff_backfill` | Timer, every 10 min | Advance each scope's backfill checkpoint by bounded date windows within the non-reserved rate budget | Take budget from `reserve_for_live` |
| `worker` | Timer, every 1 min | Claim up to N jobs (`FOR UPDATE SKIP LOCKED`, 5-min lease); run `ff_fetch`, `process_version` or `withdraw`; stop before about 8 minutes | Publish when the job's `config_revision` is no longer active |
| `maintenance` | Timer, hourly | Requeue expired leases; dead-letter after `max_attempts`; purge superseded/withdrawn chunks, segments and Blobs; reset rate windows; emit metrics | Purge anything still under an approved hold |
| `admin_mode` | HTTP, Entra-protected, operator app role only | Validate and activate a new `runtime_config` revision. Runs as a separate database login with `UPDATE` on `runtime_config` only | Be callable by the search app or by the ingestion identity's normal database role |

### Job types

| Job | Steps | Idempotency |
|---|---|---|
| `ff_fetch` | Check revision and scope → spend budget → GraphQL fetch → stage raw JSON → compute `source_revision` → insert version (or finish, if an identical version exists) → enqueue `process_version` | `UNIQUE (communication_id, source_revision, profile)` |
| `process_version` | Normalize → screen → chunk → embed → publish in one transaction | Version state machine. Chunk inserts are keyed `(version_id, chunk_kind, seq)` |
| `withdraw` | Tombstone → `lifecycle_state` → clear `active_version_id` → purge derivatives | Tombstone primary key |

Retries use exponential backoff with jitter (`run_after`). A 429 from Fireflies or Azure OpenAI pushes `run_after` past the provider's reset and does not count as a failed attempt. Error codes are content-free (`ff_http_429`, `pii_service_timeout`, …).

### Mode checks (fail closed)

Every function reads the active `runtime_config` at start, and again immediately before a source read and before publication. Missing, `paused` or provider-not-enabled means stop without error noise. A job whose `config_revision` differs from the active revision is cancelled at publication, not published into another dataset.

## 3. Search App Functions

| Function | Route | Purpose |
|---|---|---|
| `search` | `POST /api/search` | Hybrid search with citations (see the API contract in [Search and Agents](SEARCH_AND_AGENTS.md)) |
| `get_meeting` | `GET /api/meetings/{id}` | Metadata, participants and the provider summary for one authorized meeting |
| `get_transcript_window` | `GET /api/meetings/{id}/transcript?start_ms=&end_ms=` | Bounded transcript excerpt around a citation (maximum 10 minutes per call) |
| `mcp` | `/mcp` (Streamable HTTP) | The same four operations as MCP tools for agents |
| `health` | `GET /api/health` | Liveness only; no data |

All routes except health require an Entra v2 access token for the `app-fs-ci-search-<env>` audience. Validation checks the issuer or tenant, the audience, expiry and the delegated scope `Communications.Search`. Authorization comes from the token's `roles` claim and `oid` (see [Search and Agents §4](SEARCH_AND_AGENTS.md#4-authorization)). The app uses the `ci_search` database role, which can read only the `published_*` views.

## 4. Code Layout

```
apps/
  ingestion/   function_app.py  host.json  requirements.txt
  search/      function_app.py  host.json  requirements.txt
packages/
  ci_core/         config + mode checks, DB pool (Entra token auth), jobs/lease API, rate budget, logging
  ci_fireflies/    GraphQL client, webhook verification, windowed paging, normalizer
  ci_processing/   screening adapters, chunker, embedding client, publisher
  ci_retrieval/    token validation, grant resolution, hybrid query, MCP tool definitions
db/
  migrations/      0001_foundation.sql …
  tests/           SQL behaviour checks
tests/             unit + contract tests with synthetic Fireflies fixtures
infra/             Bicep (later PR)
```

Each app's build step installs the `packages/*` it needs into that app's artifact (for example `pip install --target .python_packages/lib/site-packages ../../packages/ci_core …`). A sibling folder in the repository is not available inside a deployed Function App unless it is packaged.

## 5. Identities and Permissions

| Resource | Ingestion identity | Search identity |
|---|---|---|
| PostgreSQL | Entra principal, member of `ci_ingest` | Entra principal, member of `ci_search` (published views, insert-only audit) |
| Evidence Blob | Storage Blob Data Contributor on the evidence container | None |
| Key Vault | Key Vault Secrets User (Fireflies API key, webhook secret) | None |
| Azure OpenAI | Cognitive Services OpenAI User (embeddings) | Cognitive Services OpenAI User (query embeddings) |
| Azure AI services (Language PII, Content Safety) | Cognitive Services User | None |
| Host storage | Identity-based `AzureWebJobsStorage` roles per Flex Consumption documentation | Same, on its own host storage |

PostgreSQL uses Microsoft Entra authentication only. Apps request a token for `https://ossrdbms-aad.database.windows.net/.default` via `azure-identity` and use it as the connection password, refreshing before the roughly one-hour expiry. No database password exists in app settings. Migrations run from GitHub Actions under a separate migration principal.

Secrets are app settings that reference Key Vault (`@Microsoft.KeyVault(SecretUri=…)`), so code reads an environment variable and never calls Key Vault directly.

## 6. Pilot Resource Inventory

| Resource | Development (synthetic data) | Production stack (real meetings, starts in `test` mode) |
|---|---|---|
| Function Apps ×2 + host storage | Yes | Yes |
| Evidence storage account (private, no anonymous access) | Yes | Yes |
| PostgreSQL Flexible Server, Burstable tier, pgvector, Entra auth | Yes, public endpoint with Entra-only auth and firewall | Yes, **private access (VNet)** |
| VNet + Function VNet integration + private DNS | No | Yes. Real transcripts do not sit behind a public database endpoint |
| Azure OpenAI embedding deployment | Yes | Yes |
| Azure AI services (Language PII + Content Safety) | Yes | Yes |
| Key Vault | Yes | Yes |
| Application Insights + Log Analytics | Yes | Yes, with alerts |
| Azure AI Search | **Deferred** (pgvector; ADR-012) | Deferred |
| App Configuration | **Deferred** (mode in PostgreSQL; ADR-011) | Deferred |
| Document Intelligence, Defender for Storage | Email release | Email release |

The main fixed costs are PostgreSQL and the search app's always-ready instance. Embedding and screening costs at about 42 hours of transcripts are small, but confirm them with the Azure pricing calculator for the chosen region before backfill.

## 7. Observability

Log IDs, counts, durations, states and reason codes, never transcript text, titles or query text. Custom metrics: jobs by state, oldest queued job age, dead-letter count, rate-budget use, webhook receipts by outcome, versions held by reason, publish latency (meeting end to searchable) and search latency. Alerts: dead letters > 0, oldest job > 30 min, reconcile not run in 2 h, and signature failures spiking.

## 8. Deployment

GitHub Actions with OIDC as in [ADR-007](ARCHITECTURE_DECISIONS.md#adr-007-github-owns-deployable-releases): a PR runs lint, unit and contract tests plus the SQL checks against a PostgreSQL+pgvector service container. Merging to `main` deploys development. Production release is manual behind the `production` environment. Migrations are a separate job before app deployment. Deploying code never changes `runtime_config`.
