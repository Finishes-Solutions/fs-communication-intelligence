# Local development shortcuts. Needs Docker and uv. See local/README.md.
COMPOSE := docker compose -f local/docker-compose.yml
CI := uv run --env-file local/local.env ci

.PHONY: dev up migrate seed status test check down reset

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
