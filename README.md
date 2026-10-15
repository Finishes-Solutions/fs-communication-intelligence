# Communication Intelligence

Internal Finishes Solutions project. Status: early development.

## Development

Requires [uv](https://docs.astral.sh/uv/) and, for the database checks, Docker.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy && uv run mypy tests db/run_checks.py db/migrate.py
```

Database checks (throwaway local container only):

```bash
docker run -d --name ci-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 pgvector/pgvector:0.8.2-pg18
PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres python3 db/run_checks.py
```

Local testkit (synthetic mail end to end, with a web UI): see [`local/README.md`](local/README.md).

```bash
uv run ci up
```

End-to-end tests against a throwaway database: `CI_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/ci_pytest uv run pytest -m db`.

Contributors: see `CLAUDE.md` and `docs/`.
