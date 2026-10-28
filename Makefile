# Local development shortcuts. Needs Docker and uv. See local/README.md.
COMPOSE := docker compose -f local/docker-compose.yml
CI := uv run --env-file local/local.env ci

.PHONY: dev up migrate seed status test check down reset azure-env azure-env-check azure-migrate azure-db-principals

dev: up migrate seed status  ## start everything, apply migrations, load fixtures

up:
	$(COMPOSE) up -d --wait db azurite

migrate:  ## db/migrate.py on the host, psql inside the container
	CI_PSQL="$(COMPOSE) exec -T db psql -U postgres" PGDATABASE=ci_local python3 db/migrate.py apply

seed:
	$(CI) seed

status:
	$(CI) mode show
	$(CI) scope list

test:  ## unit, contract and database tests against the local stack
	uv run --env-file local/local.env pytest

check: test  ## everything CI runs on Python code
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run mypy tests db/run_checks.py db/migrate.py

down:
	$(COMPOSE) down

reset:  ## delete the local database and storage volumes
	$(COMPOSE) down -v

# --- Azure dev database (docs/RUN_24H_TEST.md) ------------------------------
AZ_ENV := local/azure-dev.env
AZ_PSQL := docker run --rm -i -e PGHOST -e PGPORT -e PGUSER -e PGPASSWORD -e PGSSLMODE pgvector/pgvector:0.8.2-pg18 psql

# The database settings for the Azure targets below, written from your Azure CLI
# sign-in (az login). Never overwrites an existing file (it may hold Graph and AI
# settings too; local/azure-dev.env.example lists them).
azure-env:  ## write local/azure-dev.env for the Azure dev database
	@if [ -f $(AZ_ENV) ]; then echo "$(AZ_ENV) already exists; leaving it as it is"; exit 0; fi; \
	user="$$(az ad signed-in-user show --query userPrincipalName -o tsv)" && \
	tenant="$$(az account show --query tenantId -o tsv)" && [ -n "$$user" ] && [ -n "$$tenant" ] || \
	  { echo "Sign in first: az login"; exit 1; }; \
	printf '%s\n' DEPLOYMENT_ENVIRONMENT=development DATABASE_AUTH=entra \
	  PGHOST=psql-fs-ci-dev-1026b.postgres.database.azure.com PGPORT=5432 "PGUSER=$$user" \
	  PGDATABASE=ci PGSSLMODE=require "GRAPH_TENANT_ID=$$tenant" AUTH_MODE=entra > $(AZ_ENV) && \
	echo "wrote $(AZ_ENV) for $$user"

azure-env-check:
	@[ -f $(AZ_ENV) ] || { echo "$(AZ_ENV) is missing. Create it with: make azure-env"; exit 1; }

azure-migrate: azure-env-check  ## create the database if needed and apply migrations, as the signed-in Azure CLI user
	@set -a; . ./$(AZ_ENV); set +a; \
	export PGPASSWORD="$$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"; \
	$(AZ_PSQL) -X -q -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$$PGDATABASE'" | grep -q 1 || \
	  $(AZ_PSQL) -X -q -d postgres -c "CREATE DATABASE \"$$PGDATABASE\"" && \
	CI_PSQL="$(AZ_PSQL)" python3 db/migrate.py apply && \
	$(AZ_PSQL) -X -q -v ON_ERROR_STOP=1 -d "$$PGDATABASE" \
	  -c 'GRANT ci_ingest, ci_search, ci_operator TO CURRENT_USER' \
	  -c "SELECT 'pgvector ' || extversion FROM pg_extension WHERE extname = 'vector'"

azure-db-principals: azure-env-check  ## database logins for the managed identities (run after azure-migrate)
	@set -a; . ./$(AZ_ENV); set +a; \
	export PGPASSWORD="$$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"; \
	ADMIN_MI=; az identity show -g fs-communication-intelligence -n id-fs-ci-admin-dev -o none 2>/dev/null && ADMIN_MI=id-fs-ci-admin-dev; \
	for mi in id-fs-ci-ingestion-dev id-fs-ci-search-dev $$ADMIN_MI; do \
	  $(AZ_PSQL) -X -q -v ON_ERROR_STOP=1 -d postgres -c \
	    "DO \$$\$$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$$mi') THEN PERFORM pgaadauth_create_principal('$$mi', false, false); END IF; END \$$\$$;" || exit 1; \
	done; \
	if [ -n "$$ADMIN_MI" ]; then $(AZ_PSQL) -X -q -v ON_ERROR_STOP=1 -d "$$PGDATABASE" \
	  -c 'GRANT ci_operator, ci_search TO "id-fs-ci-admin-dev"' || exit 1; fi; \
	$(AZ_PSQL) -X -q -v ON_ERROR_STOP=1 -d "$$PGDATABASE" \
	  -c 'GRANT ci_ingest TO "id-fs-ci-ingestion-dev"' -c 'GRANT ci_search TO "id-fs-ci-search-dev"' \
	  -c "SELECT r.rolname AS login, string_agg(g.rolname, ', ') AS app_roles FROM pg_roles r JOIN pg_auth_members m ON m.member = r.oid JOIN pg_roles g ON g.oid = m.roleid WHERE r.rolname LIKE 'id-fs-ci-%' GROUP BY 1"
