"""Apply every migration to a throwaway database and run the SQL behavior checks.

Usage (needs psql and a THROWAWAY PostgreSQL + pgvector server; standard PG*
environment variables select it, and the user must be a superuser):

    python db/run_checks.py

The check files print lines like ``visible before publish (expect 0): 0`` and
raise an error when a permission check fails. This script fails if any SQL
errors, any ``(expect N): M`` line has M != N, or any NOTICE is not ``OK:``.

It drops and recreates the database named by ``CI_CHECK_DB`` (default
``ci_check``) and the cluster-level ``ci_*``/``t_*`` roles the checks create.
Never point it at a real server.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent
EXPECT = re.compile(r"\(expect (\d+)\): (\d+)\s*$")
NOTICE = re.compile(r"^(?:psql:[^ ]+ )?NOTICE:\s+(.*)$")
ROLES = ["t_ingest", "t_search", "t_operator", "ci_ingest", "ci_search", "ci_operator"]


def psql(database: str, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", database, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603


def must(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        sys.stderr.write(f"FAIL: {what}\n{result.stdout}{result.stderr}")
        raise SystemExit(1)
    return result.stdout + result.stderr


def reset(database: str) -> None:
    must(psql("postgres", "-c", f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'), "drop db")
    for role in ROLES:
        must(psql("postgres", "-c", f'DROP ROLE IF EXISTS "{role}"'), f"drop role {role}")
    must(psql("postgres", "-c", f'CREATE DATABASE "{database}"'), "create db")


def main() -> int:
    database = os.environ.get("CI_CHECK_DB", "ci_check")
    reset(database)

    for migration in sorted((DB_DIR / "migrations").glob("*.sql")):
        must(psql(database, "-f", str(migration)), f"apply {migration.name}")
        print(f"applied {migration.name}")

    version = must(
        psql(database, "-tA", "-c", "SELECT extversion FROM pg_extension WHERE extname='vector'"),
        "read pgvector version",
    ).strip()
    server = must(psql(database, "-tA", "-c", "SHOW server_version"), "read version").strip()
    print(f"PostgreSQL {server}, pgvector {version}")

    failures: list[str] = []
    for check in sorted((DB_DIR / "tests").glob("*.sql")):
        output = must(psql(database, "-tA", "-f", str(check)), f"run {check.name}")
        passed = 0
        for line in output.splitlines():
            if m := EXPECT.search(line):
                if m.group(1) == m.group(2):
                    passed += 1
                else:
                    failures.append(f"{check.name}: {line.strip()}")
            elif m := NOTICE.match(line.strip()):
                if m.group(1).startswith("OK:"):
                    passed += 1
                else:
                    failures.append(f"{check.name}: NOTICE {m.group(1)}")
        print(f"{check.name}: {passed} assertions passed")
        if passed == 0:
            failures.append(f"{check.name}: no assertions found")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
