"""PostgreSQL connections.

Connection details come from the standard libpq ``PG*`` environment variables
(``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``, ``PGDATABASE``), so the same
code works against the local container and, later, Azure (where PR 8 supplies
an Entra access token as the password). Every session uses ``search_path = ci``.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import DictRow, dict_row

Connection = psycopg.Connection[DictRow]


def required(row: DictRow | None) -> DictRow:
    """The row a statement must return (e.g. ``INSERT ... RETURNING``)."""
    if row is None:
        raise RuntimeError("statement returned no row")
    return row


def connect(conninfo: str = "", **kwargs: Any) -> Connection:
    return psycopg.connect(
        conninfo, row_factory=dict_row, options="-c search_path=ci,public", **kwargs
    )
