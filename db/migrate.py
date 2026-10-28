"""Apply pending SQL migrations in order and record them in ci.schema_migrations.

Usage (standard PG* environment variables select the server and database):

    python db/migrate.py status   # list applied and pending migrations
    python db/migrate.py apply    # apply pending migrations

Each file in db/migrations named NNNN_name.sql runs in its own transaction
together with its ledger row, so a failed migration leaves nothing behind.
Files must not contain BEGIN/COMMIT. If an applied file's checksum no longer
matches the ledger, the runner stops: never edit an applied migration, add a
new one instead.

SQL is sent to psql on standard input, so ``CI_PSQL`` can name a psql inside a
container (``make migrate`` uses ``docker compose exec -T db psql -U postgres``)
and no local psql install is needed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_CONTROL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", re.IGNORECASE | re.MULTILINE)

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS ci;
CREATE TABLE IF NOT EXISTS ci.schema_migrations (
    version    text PRIMARY KEY,
    filename   text NOT NULL,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL DEFAULT current_user
);
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sha256: str


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: migration files must be named NNNN_name.sql")
        text = path.read_text()
        if TRANSACTION_CONTROL.search(text):
            raise MigrationError(
                f"{path.name}: remove BEGIN/COMMIT; the runner adds the transaction"
            )
        migrations.append(
            Migration(match.group(1), path, hashlib.sha256(text.encode()).hexdigest())
        )
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version numbers")
    return migrations


def psql(database: str, *args: str, stdin: str | None = None) -> str:
    base = shlex.split(os.environ.get("CI_PSQL", "psql"))
    cmd = [*base, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database, *args]
    result = subprocess.run(  # noqa: S603
        cmd, input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise MigrationError(result.stdout + result.stderr)
    return result.stdout


def applied(database: str) -> dict[str, str]:
    psql(database, "-c", LEDGER_DDL)
    rows = psql(
        database, "-tA", "-F", "\t", "-c", "SELECT version, sha256 FROM ci.schema_migrations"
    )
    return dict(line.split("\t") for line in rows.splitlines() if line)


def pending(database: str, migrations: list[Migration]) -> list[Migration]:
    ledger = applied(database)
    for m in migrations:
        if m.version in ledger and ledger[m.version] != m.sha256:
            raise MigrationError(
                f"{m.path.name} was edited after it was applied (checksum mismatch); "
                "add a new migration instead"
            )
    unknown = set(ledger) - {m.version for m in migrations}
    if unknown:
        raise MigrationError(
            f"database has migrations missing from this checkout: {sorted(unknown)}"
        )
    return [m for m in migrations if m.version not in ledger]


def apply(database: str, migrations: list[Migration] | None = None) -> list[Migration]:
    todo = pending(database, discover() if migrations is None else migrations)
    for m in todo:
        # psql -c does not bind parameters, so the values are checked here before
        # they are written into the statement.
        if not (FILENAME.match(m.path.name) and m.version.isdigit() and SHA256.match(m.sha256)):
            raise MigrationError(f"refusing unexpected ledger values for {m.path.name}")
        record = (
            "INSERT INTO ci.schema_migrations (version, filename, sha256) "  # noqa: S608
            f"VALUES ('{m.version}', '{m.path.name}', '{m.sha256}')"
        )
        psql(database, "--single-transaction", "-f", "-", "-c", record, stdin=m.path.read_text())
    return todo


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    database = os.environ.get("PGDATABASE", "postgres")
    try:
        if command == "status":
            migrations = discover()
            todo = {m.version for m in pending(database, migrations)}
            for m in migrations:
                print(f"{'pending' if m.version in todo else 'applied'}  {m.path.name}")
        elif command == "apply":
            done = apply(database)
            for m in done:
                print(f"applied  {m.path.name}")
            print(f"{len(done)} migration(s) applied")
        else:
            print(__doc__)
            return 2
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
