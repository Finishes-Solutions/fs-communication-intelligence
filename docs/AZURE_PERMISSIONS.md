# Azure, Entra and Exchange Permissions (dev)

Status: the contract for the hand-built dev environment (CLAUDE.md §10.3). It mirrors Appendix C of the *FS CI Azure Build Guide (Dev), revision 2*, which is kept outside this public repository. **Nothing here has been verified in the tenant yet.** If a grant is not in this file, it should not exist. A pull request that needs a new grant adds it here first and says why.

Never grant `Mail.Read`, `Mail.ReadWrite`, `Mail.Send` or any other `Mail.*` **application** permission in Entra, to any identity (L5, L6). It would silently override the Exchange scope.

## 1. Microsoft Graph, Exchange and the search API

| Identity | Permission | Type and purpose | Guide part |
|---|---|---|---|
| `id-fs-ci-ingestion-dev` | `User.Read.All` | Graph application. Mailbox inventory (users and shared mailboxes). Cannot read mail. | 12.6 |
| `id-fs-ci-ingestion-dev` | `Application Mail.Read` over management scope `FS-CI-Email-Dev` | Exchange RBAC for Applications. Messages, folders, delta and attachment metadata, only for members of the scope group. | 12.4 |
| `app-fs-ci-local-reader-dev` (certificate) | `User.Read.All`, admin consent | Graph application. Same inventory from a developer Mac. | 11.4 |
| `app-fs-ci-local-reader-dev` | `Application Mail.Read` over `FS-CI-Email-Dev` | Exchange RBAC. Reads the test mailbox from a developer Mac. | 12.4 |
| `app-fs-ci-test-sender` (public client) | `Mail.Send`, `User.Read` | Graph **delegated**. Sends synthetic test mail as the signed-in person; assignment required, only Matthew and Cody. | 11.5 |
| `app-fs-ci-client-{claude,copilot,chatgpt}-dev` | `Communications.Search`, `openid`, `profile`, `offline_access` | Delegated, admin consent. Each AI platform calls the search API as the signed-in user. | 11.3 |
| `app-fs-ci-search-dev` (API) | None. Exposes `Communications.Search` (admins-only consent) and app roles `Communications.Reader.All` / `Communications.Reader.Own`; token version 2; assignment required | Validates tokens; calls no Microsoft API. Optionally pre-authorizes the Azure CLI client (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) for dev testing. | 11.1–11.2 |
| `id-fs-ci-search-dev`, `id-fs-ci-github-dev` | None | No Graph or Exchange access. | — |

Enterprise application assignments: `app-fs-ci-search-dev` → `SG-FS-CI-Readers-All` as `Communications.Reader.All`; `app-fs-ci-test-sender` → Matthew and Cody.

**Open verification (PR 5):** Microsoft documents RBAC for Applications as covering Graph mail reads. Whether Graph change-notification subscriptions on `users/{id}/messages` are honored under the Exchange-scoped grant has not been tested. The capture PR must check it with a real call against one in-scope and one out-of-scope mailbox, and fall back to delta-only capture if it fails. It must not "fix" a failure by adding an Entra `Mail.Read` grant.

Deferred to E2, with security approval: `ThreatHunting.Read.All`.

## 2. Azure role assignments

| Who | Role | Resource | Guide part |
|---|---|---|---|
| `id-fs-ci-github-dev` | Contributor; Role Based Access Control Administrator | Resource group `fs-communication-intelligence` (not the subscription) | 2 |
| `id-fs-ci-ingestion-dev` | Storage Blob Data Owner; Storage Queue Data Contributor; Storage Table Data Contributor | `stfsciingdev<sfx>` (its Functions host storage) | 10.3 |
| `id-fs-ci-ingestion-dev` | Storage Blob Data Contributor | `evidence` container in `stfscievidev<sfx>` | 6.2 |
| `id-fs-ci-ingestion-dev` | Key Vault Secrets User | `kv-fs-ci-dev-<sfx>` (Graph webhook `clientState`) | 5 |
| `id-fs-ci-ingestion-dev` | Cognitive Services OpenAI User | `aoai-fs-ci-dev-<sfx>` (embedding + `classifier` deployments) | 8.4 |
| `id-fs-ci-ingestion-dev` | Cognitive Services User | `lang-fs-ci-dev-<sfx>`, `safe-fs-ci-dev-<sfx>` | 9 |
| `id-fs-ci-search-dev` | Storage Blob Data Owner; Storage Queue Data Contributor; Storage Table Data Contributor | `stfscisrchdev<sfx>` (its Functions host storage) | 10.3 |
| `id-fs-ci-search-dev` | Cognitive Services OpenAI User | `aoai-fs-ci-dev-<sfx>` (embeds the query) | 8.4 |
| `SG-FS-CI-Developers` | Cognitive Services OpenAI User | `aoai-fs-ci-dev-<sfx>` | 8.4 |
| `SG-FS-CI-Developers` | Cognitive Services User | `lang-fs-ci-dev-<sfx>`, `safe-fs-ci-dev-<sfx>` | 9 |
| Matthew | Key Vault Administrator | `kv-fs-ci-dev-<sfx>` | 5 |
| Matthew (optional) | Storage Blob Data Reader | `evidence` container | 6.2 |

The search identity has no role on the evidence account, Key Vault, Language or Content Safety (L4). Its host-storage roles are required by the Functions runtime and never hold email. API keys are disabled on the Azure OpenAI, Language and Content Safety resources, and account-key access is disabled on the evidence storage account.

## 3. Database (PostgreSQL 18 Flexible Server, Entra-only)

| Login | Member of | Can |
|---|---|---|
| `id-fs-ci-ingestion-dev` | `ci_ingest` | Read all tables; write evidence, jobs, checkpoints; record observed scope state. Not mode, approvals, grants or exclusions. |
| `id-fs-ci-search-dev` | `ci_search` | Read `published_*` views, `access_grants`, `corpora`, `runtime_config`; append audit. No base tables. |
| `id-fs-ci-github-dev` | none | Migration login: `CREATE, CONNECT` on database `communications`; owns schema `ci` and its objects. No `CREATEROLE`. |
| `SG-FS-CI-PG-Admins` (Entra admin group) | `ci_operator` | Server administration; operator commands (mode, scope, exclusions, grants). |

Created by hand in guide §7.4, as the admin group: the three `pgaadauth_create_principal` logins; extensions `vector`, `pgcrypto`, `pg_trgm` (all three allow-listed in `azure.extensions`); roles `ci_ingest`, `ci_search`, `ci_operator` and the memberships above. Everything else comes from `db/migrations`. Role permissions are described in [DATABASE.md §6](DATABASE.md#6-database-roles).

## 4. People performing setup

See guide §0.1: subscription Owner (or Contributor + RBAC Administrator on the resource group), Global Administrator (or Privileged Role Administrator + Application Administrator) for app registrations and consent, Exchange Administrator for the shared mailbox, scope groups and RBAC, and repository admin for GitHub environments.
