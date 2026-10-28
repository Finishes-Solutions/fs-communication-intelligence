"""Database tests run against a throwaway database on the server named by PG* variables.

They are skipped when no server is reachable, unless ``CI_REQUIRE_DB=1`` (set in
CI), which turns a missing server into a failure. The database named by
``CI_PYTEST_DB`` (default ``ci_pytest``) is dropped and recreated for each test
module, so never point PG* variables at a real server when running tests.
"""

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from ci_core.db import Connection, connect

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "db"))
import migrate

DB_NAME = os.environ.get("CI_PYTEST_DB", "ci_pytest")


def _admin() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(dbname="postgres", autocommit=True, connect_timeout=3)


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    try:
        admin = _admin()
    except psycopg.OperationalError as exc:
        if os.environ.get("CI_REQUIRE_DB") == "1":
            pytest.fail(f"CI_REQUIRE_DB=1 but no database server is reachable: {exc}")
        pytest.skip("no PostgreSQL server reachable through PG* variables")
    with admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    migrate.apply(DB_NAME)
    yield DB_NAME


@pytest.fixture
def conn(database: str) -> Iterator[Connection]:
    """A connection whose work is rolled back after each test."""
    with connect(dbname=database) as c:
        yield c
        c.rollback()
