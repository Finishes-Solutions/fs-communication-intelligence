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

import logging
import os
import time
from collections.abc import Callable
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

log = logging.getLogger(__name__)

Connection = psycopg.Connection[DictRow]
AppRole = Literal["ci_ingest", "ci_search", "ci_operator"]

POSTGRES_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# A dropped connection must fail, not hang a worker forever: TCP keepalives
# notice a dead server in about a minute, and connecting gives up after 20 s.
KEEPALIVE = {
    "connect_timeout": 20,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}
# Per-role limits on one statement and on a transaction left waiting: processing
# holds a transaction open while it calls the AI services (with retries), so its
# limits are generous; search answers people and agents, so it fails fast.
LIMITS: dict[str, tuple[str, str]] = {
    "ci_ingest": ("15min", "30min"),
    "ci_operator": ("5min", "10min"),
    "ci_search": ("2min", "5min"),
}


def required(row: DictRow | None) -> DictRow:
    """The row a statement must return (e.g. ``INSERT ... RETURNING``)."""
    if row is None:
        raise RuntimeError("statement returned no row")
    return row


def _entra_password() -> str:
    from ci_core.azure_auth import default_credential

    return str(default_credential().get_token(POSTGRES_ENTRA_SCOPE).token)


# Opening a connection is retried this many times on a connection timeout, with a
# growing pause: under heavy load (the 2026-11-03 backfill) the server sometimes takes
# longer than connect_timeout to accept one, and a capture week opens one per mailbox,
# so a single slow handshake used to fail the whole week.
CONNECT_ATTEMPTS = 4
CONNECT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)


def connect(
    conninfo: str = "",
    *,
    role: AppRole | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Connection:
    if os.environ.get("DATABASE_AUTH", "password").lower() == "entra":
        kwargs.setdefault("password", _entra_password())
        kwargs.setdefault("sslmode", os.environ.get("PGSSLMODE", "require"))
    for key, value in KEEPALIVE.items():
        kwargs.setdefault(key, value)
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            conn = psycopg.connect(
                conninfo, row_factory=dict_row, options="-c search_path=ci,public", **kwargs
            )
            break
        except psycopg.errors.ConnectionTimeout:
            if attempt == CONNECT_ATTEMPTS - 1:
                raise
            pause = CONNECT_BACKOFF_SECONDS[min(attempt, len(CONNECT_BACKOFF_SECONDS) - 1)]
            log.warning("database connection timed out; retrying in %.0f s", pause)
            sleep(pause)
    if role is not None:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        statement, idle = LIMITS[role]
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (statement,))
        conn.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, false)", (idle,))
        conn.commit()
    return conn
