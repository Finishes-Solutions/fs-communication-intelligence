# Deployment (dev)

Status: workflows written and checked offline (actionlint, unit tests, a local build, and a migration rehearsal as a non-admin login against PostgreSQL 16 + pgvector 0.8.2). **They have not yet run against Azure.** The first run on `main` is the real test; see §5.

## 1. What runs when

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | every pull request, every push to `main`, and called by `deploy-dev.yml` | lint, format, type check, unit tests, SQL migrations and checks on PostgreSQL 18 + pgvector 0.8.2 |
| `deploy-dev.yml` | every push to `main`; manual run | runs CI, decides what changed, then migrates and/or deploys only that |
| `deploy-function-app.yml` | called by `deploy-dev.yml` | builds one app, deploys it, smoke-tests `/api/health` |

Nothing deploys from a pull request or a feature branch. Production has no workflow yet (CLAUDE.md §13: production releases are manual and Matthew's call).

## 2. What a change redeploys

`deploy-dev.yml` compares the commit being deployed with the commit of the **last successful** `deploy-dev.yml` run on `main` (`scripts/deploy_targets.py`). Comparing with the last success, not the previous push, means a failed deploy is retried by the next push instead of being forgotten.

| Changed path | Ingestion | Search | Migrations |
|---|---|---|---|
| `apps/ingestion/**` | yes | | |
| `apps/search/**` | | yes | |
| `packages/<name>/**` | if listed in `apps/ingestion/packages.txt` | if listed in `apps/search/packages.txt` | |
| `pyproject.toml`, `uv.lock`, `scripts/build_function_app.py`, `deploy-dev.yml`, `deploy-function-app.yml` | yes | yes | |
| `db/migrations/**`, `db/migrate.py` | | | yes |
| docs, tests, fixtures, `db/tests`, `config/`, `ci.yml`, other scripts | | | |

Today `ci_core` goes to both apps, `ci_email` and `ci_processing` to ingestion only, and `ci_retrieval` to search only. `ci_email` must never be listed for search (L4); a test enforces that. When an app starts importing another `ci_*` package, add it to that app's `packages.txt`; `tests/unit/test_function_apps.py` fails until you do.

No previous successful run (the first deploy), rewritten history, or a manual run with **scope = all** deploys everything.

Order: CI must pass. Migrations run before the apps, and an app deploy is skipped if migrations failed. The two apps deploy in parallel. Only one deploy runs at a time; a newer push waits rather than cancelling a deploy halfway.

## 3. How an app is built and verified

`scripts/build_function_app.py <app>` assembles `build/<app>/`:

- `function_app.py` and `host.json` from `apps/<app>/`;
- the shared packages listed in `apps/<app>/packages.txt`;
- locked dependencies (`uv export` from `uv.lock`, main dependencies plus the `functions` group) installed into `.python_packages/lib/site-packages` as **Linux x86_64 CPython 3.14 wheels**, so a build on a Mac matches the CI build;
- `build_info.json` with the commit.

The package is deployed as built; Azure does no remote build (ADR-018). After deploying, the workflow polls `https://<app>/api/health` until it reports the commit just deployed (up to 5 minutes). A deploy that "succeeds" but leaves old code running therefore fails.

`/api/health` is anonymous and returns only `{"status", "app", "git_sha"}`: no data, no settings.

Build locally (needs `uv`):

```bash
uv run python scripts/build_function_app.py ingestion
```

## 4. Configuration it relies on

GitHub › Settings › Environments › **development** › variables (build guide Part 3). All are non-secret; there are no stored credentials. Azure sign-in uses OIDC as `id-fs-ci-github-dev`.

| Variable | Used for |
|---|---|
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | OIDC sign-in |
| `AZURE_RESOURCE_GROUP` | firewall rule, app host name lookup |
| `CI_PG_HOST` | migrations (server name is its first label) |
| `CI_FUNC_INGEST_NAME`, `CI_FUNC_SEARCH_NAME` | deploy targets |

Environment variables of `development` and repository variables both work, because every job that reads them runs in the `development` environment.

Migrations sign in to PostgreSQL as `id-fs-ci-github-dev` with an Entra token, after adding a firewall rule for the runner's IP; the rule is removed in an `always()` step. The roles `ci_ingest`, `ci_search` and `ci_operator` must already exist (build guide §7.4; the migration login cannot create roles).

App settings (endpoints, `PG_*`, …) are not set by these workflows; they come from the build guide §10.4 / the build script.

## 5. First run checklist and what is still assumed

Before the first push to `main` with these workflows:

1. The development environment variables in §4 are set; `CI_PG_HOST` is the actual server (`psql-fs-ci-dev-1026b…` if the rename was used).
2. `id-fs-ci-github-dev` has its federated credential for `environment:development` and Contributor on the resource group (bootstrap script).
3. Both Function Apps exist on Python 3.14.

Verified offline (this PR): unit tests for the change rules and the package layout; a real build of the ingestion package (Linux cp314 wheels, health function imports and answers from the built folder alone); actionlint on all workflows; migrations `0001`–`0002` applied as a non-superuser `id-fs-ci-github-dev` with pre-created roles on PostgreSQL 16 + pgvector 0.8.2, followed by role checks (search cannot read base tables or write; ingestion cannot change `runtime_config`; the admin group cannot read chunks).

Assumed until the first run proves it:

- Flex Consumption loads dependencies from `.python_packages/lib/site-packages` in a package deployed without remote build. Microsoft documents that path for Python apps generally; the Flex pages do not restate it. The smoke test fails loudly if it does not hold.
- `Azure/functions-action@v1` with OIDC resolves the Flex Consumption plan on its own (its README says so).
- The runner's public IP seen by `api.ipify.org` is the one PostgreSQL sees.
- The same migration rehearsal on PostgreSQL 18 in Azure (the local rehearsal used 16; the privilege rules involved are the same since 16).
