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

``pooled(role)`` lends a connection that is kept open and reused by later
requests (``ConnectionPool``). The search app and the admin console use it:
opening a connection to Azure costs a TLS handshake, an Entra token and four
setup statements, which every request used to pay.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Any, Literal

import psycopg
from psycopg import pq, sql
from psycopg.rows import DictRow, dict_row

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


def connect(conninfo: str = "", *, role: AppRole | None = None, **kwargs: Any) -> Connection:
    if os.environ.get("DATABASE_AUTH", "password").lower() == "entra":
        kwargs.setdefault("password", _entra_password())
        kwargs.setdefault("sslmode", os.environ.get("PGSSLMODE", "require"))
    for key, value in KEEPALIVE.items():
        kwargs.setdefault(key, value)
    conn = psycopg.connect(
        conninfo, row_factory=dict_row, options="-c search_path=ci,public", **kwargs
    )
    if role is not None:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        statement, idle = LIMITS[role]
        conn.execute("SELECT set_config('statement_timeout', %s, false)", (statement,))
        conn.execute("SELECT set_config('idle_in_transaction_session_timeout', %s, false)", (idle,))
        conn.commit()
    return conn


log = logging.getLogger(__name__)


class ConnectionPool:
    """Open connections kept per application role and lent one request at a time.

    A lent connection behaves like ``with connect(role=...) as conn``: its work is
    committed when the block ends normally and rolled back when it raises. Then,
    instead of closing, it goes back to the pool if it is healthy and idle. Only
    the role's own settings (``SET ROLE``, the role's timeouts) carry over between
    requests: everything else this code sets is local to a transaction.

    A connection is closed instead of reused when it is older than ``max_age``
    seconds (it was opened with an Entra token, and Azure may restart or move the
    server), when it is broken or left in a transaction, or when ``max_idle``
    connections of its role are already waiting. One left unused for more than
    ``ping_after`` seconds is checked with ``SELECT 1`` before it is lent.
    ``max_idle=0`` turns pooling off: every request opens and closes its own.
    """

    def __init__(
        self,
        opener: Callable[[AppRole], Connection] | None = None,
        *,
        max_idle: int = 4,
        max_age: float = 1800.0,
        ping_after: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._open = opener or (lambda role: connect(role=role))
        self.max_idle = max(0, max_idle)
        self.max_age = max_age
        self.ping_after = ping_after
        self._clock = clock
        self._lock = threading.Lock()
        # role -> [(connection, opened at, last returned at)], newest last
        self._idle: dict[str, list[tuple[Connection, float, float]]] = {}
        self.opened = 0  # connections opened over the pool's life (for tests and logs)

    @contextmanager
    def connection(self, role: AppRole) -> Iterator[Connection]:
        conn, opened_at = self._take(role)
        try:
            yield conn
        except BaseException:
            self._give_back(role, conn, opened_at, success=False)
            raise
        self._give_back(role, conn, opened_at, success=True)

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for entries in idle.values():
            for conn, _, _ in entries:
                _close_quietly(conn)

    def idle_count(self, role: AppRole) -> int:
        with self._lock:
            return len(self._idle.get(role, []))

    def _take(self, role: AppRole) -> tuple[Connection, float]:
        while True:
            with self._lock:
                entries = self._idle.get(role)
                entry = entries.pop() if entries else None
            if entry is None:
                break
            conn, opened_at, returned_at = entry
            now = self._clock()
            if conn.closed or conn.broken or now - opened_at > self.max_age:
                _close_quietly(conn)
                continue
            if now - returned_at > self.ping_after:
                try:
                    conn.execute("SELECT 1")
                    conn.rollback()
                except psycopg.Error:
                    log.info("pooled %s connection was dead; opening another", role)
                    _close_quietly(conn)
                    continue
            return conn, opened_at
        conn = self._open(role)
        with self._lock:
            self.opened += 1
        return conn, self._clock()

    def _give_back(self, role: AppRole, conn: Connection, opened_at: float, success: bool) -> None:
        try:
            if not conn.closed:
                conn.commit() if success else conn.rollback()
        except psycopg.Error:
            if success:
                _close_quietly(conn)
                raise  # the work was not saved: the caller must know
            _close_quietly(conn)
            return
        reusable = (
            self.max_idle > 0
            and not conn.closed
            and not conn.broken
            and conn.info.transaction_status == pq.TransactionStatus.IDLE
            and self._clock() - opened_at <= self.max_age
        )
        if reusable:
            with self._lock:
                entries = self._idle.setdefault(role, [])
                if len(entries) < self.max_idle:
                    entries.append((conn, opened_at, self._clock()))
                    return
        _close_quietly(conn)


def _close_quietly(conn: Connection) -> None:
    with suppress(psycopg.Error):
        conn.close()


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def shared_pool() -> ConnectionPool:
    """The process's pool. ``DB_POOL_SIZE`` (default 4) is how many open connections
    each role keeps; 0 turns pooling off without a code change."""
    global _pool
    with _pool_lock:
        if _pool is None:
            try:
                size = int(os.environ.get("DB_POOL_SIZE", "4"))
            except ValueError:
                size = 4
            _pool = ConnectionPool(max_idle=size)
        return _pool


def pooled(role: AppRole) -> AbstractContextManager[Connection]:
    """``with pooled("ci_search") as conn:``: a connection from the shared pool."""
    return shared_pool().connection(role)
