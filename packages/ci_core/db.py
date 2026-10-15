"""PostgreSQL access with one database role per responsibility.

Every unit of work runs in a transaction under exactly one of the roles that
0001_foundation.sql defines, so the code exercises the same grants the
deployed identities will have:

- ``ci_ingest``: capture and processing (evidence, jobs, observed scope state)
- ``ci_search``: retrieval (``published_*`` views only)
- ``ci_operator``: mode, scope approvals, exclusions, grants

Locally the connection logs in as the database owner and switches role with
``SET LOCAL ROLE``; a deployed identity is a member of its role and does the same.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal, LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

DbRole = Literal["ci_ingest", "ci_search", "ci_operator"]
Connection = psycopg.Connection[DictRow]
Row = dict[str, Any]
Params = Sequence[Any] | Mapping[str, Any]


def connect(database_url: str) -> Connection:
    """Open an autocommit connection; use ``transaction`` for every unit of work."""
    return psycopg.connect(database_url, row_factory=dict_row, autocommit=True)


@contextmanager
def transaction(conn: Connection, role: DbRole) -> Iterator[Connection]:
    """Run a transaction as ``role``. Rolls back on any exception."""
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        conn.execute("SET LOCAL search_path = ci, public")
        yield conn


def one(conn: Connection, query: LiteralString, params: Params = ()) -> Row | None:
    return conn.execute(query, params).fetchone()


def one_required(conn: Connection, query: LiteralString, params: Params = ()) -> Row:
    """A single row that must exist (e.g. INSERT ... RETURNING)."""
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("expected a row, got none")
    return row


def many(conn: Connection, query: LiteralString, params: Params = ()) -> list[Row]:
    return conn.execute(query, params).fetchall()


def vector_literal(values: Sequence[float]) -> str:
    """pgvector text input format; cast with ``::vector`` in SQL."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"
