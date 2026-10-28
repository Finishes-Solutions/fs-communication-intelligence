"""PostgreSQL connections.

Connection details come from the standard libpq ``PG*`` environment variables
(``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGDATABASE`` and, locally, ``PGPASSWORD``).
With ``DATABASE_AUTH=entra`` the password is an Entra access token for Azure
Database for PostgreSQL, fetched with the signed-in Azure CLI user or the
app's managed identity, and TLS is required. Every session uses
``search_path = ci``.

``role`` switches the session to one of the application roles
(``ci_ingest``, ``ci_search``, ``ci_operator``) so code runs with that role's
grants even when the login is an administrator.
"""

from __future__ import annotations

import os
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

Connection = psycopg.Connection[DictRow]
AppRole = Literal["ci_ingest", "ci_search", "ci_operator"]

POSTGRES_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def required(row: DictRow | None) -> DictRow:
    """The row a statement must return (e.g. ``INSERT ... RETURNING``)."""
    if row is None:
        raise RuntimeError("statement returned no row")
    return row


def _entra_password() -> str:
    from ci_core.azure_auth import default_credential

    return str(default_credential().get_token(POSTGRES_ENTRA_SCOPE).token)


def connect(conninfo: str = "", *, role: AppRole | None = None, **kwargs: Any) -> Connection:
    if os.environ.get("DATABASE_AUTH", "password").lower() == "entra":
        kwargs.setdefault("password", _entra_password())
        kwargs.setdefault("sslmode", os.environ.get("PGSSLMODE", "require"))
    conn = psycopg.connect(
        conninfo, row_factory=dict_row, options="-c search_path=ci,public", **kwargs
    )
    if role is not None:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        conn.commit()
    return conn
