# Local development shortcuts. Needs Docker and uv. See local/README.md.
COMPOSE := docker compose -f local/docker-compose.yml
CI := uv run --env-file local/local.env ci

.PHONY: dev up migrate seed status test check down reset azure-migrate

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

azure-migrate:  ## create the database if needed and apply migrations, as the signed-in Azure CLI user
	@set -a; . ./$(AZ_ENV); set +a; \
	export PGPASSWORD="$$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"; \
	$(AZ_PSQL) -X -q -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$$PGDATABASE'" | grep -q 1 || \
	  $(AZ_PSQL) -X -q -d postgres -c "CREATE DATABASE \"$$PGDATABASE\"" && \
	CI_PSQL="$(AZ_PSQL)" python3 db/migrate.py apply && \
	$(AZ_PSQL) -X -q -v ON_ERROR_STOP=1 -d "$$PGDATABASE" \
	  -c 'GRANT ci_ingest, ci_search, ci_operator TO CURRENT_USER' \
	  -c "SELECT 'pgvector ' || extversion FROM pg_extension WHERE extname = 'vector'"
