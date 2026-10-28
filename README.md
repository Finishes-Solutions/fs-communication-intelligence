# Communication Intelligence

Internal Finishes Solutions project. Status: early development.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker. `make dev` starts a local stack with synthetic data only; see [`local/README.md`](local/README.md).

```bash
uv sync
make dev     # local PostgreSQL + pgvector, migrations, synthetic fixture tenant
make test    # includes database tests against the local stack
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests db/run_checks.py db/migrate.py
```

Database checks (throwaway local container only):

```bash
docker run -d --name ci-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 pgvector/pgvector:0.8.2-pg18
PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres python3 db/run_checks.py
```

Contributors: see `CLAUDE.md` and `docs/`.
