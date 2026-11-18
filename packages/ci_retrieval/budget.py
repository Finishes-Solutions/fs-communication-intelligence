"""A time budget for the database work of one MCP tool call.

A tool runs several statements. The search role's own limit is per statement, so
five statements of 30 s each would pass it and still leave the agent platform
waiting past its own limit (about a minute). Inside ``budgeted(conn)`` every
statement gets, as its ``statement_timeout``, what is left of the call's budget
(``current``), and once the budget is spent the next statement raises
``ToolBudgetSpent`` without reaching the database.

The setting is transaction-local on purpose (``set_config(..., true)``): a
session-level one is undone by ``conn.rollback()``, and a local one leaves
nothing behind on a pooled connection. It is set again at the start of each
transaction, whenever the last one is older than ``REFRESH_SECONDS``, and before
every statement in the budget's last ``REFRESH_SECONDS``. Inside one transaction
a statement may therefore run up to ``REFRESH_SECONDS`` past the budget.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Self
from weakref import WeakKeyDictionary

import psycopg
from psycopg import pq
from psycopg.rows import DictRow

# No single statement gets longer than this, however much budget is left (the
# limit tools had before the budget: ci_retrieval.mcp_server.TOOL_STATEMENT_TIMEOUT).
STATEMENT_CAP_SECONDS = 40.0
REFRESH_SECONDS = 1.0


class ToolBudgetSpent(Exception):
    """The tool call's time for database work is used up."""


class Budget:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._deadline = clock() + seconds

    def remaining(self) -> float:
        return self._deadline - self._clock()


# The running tool call's budget; None (no limit added here) outside a tool call.
current: ContextVar[Budget | None] = ContextVar("ci_tool_budget", default=None)

# connection -> when its statement_timeout was last set from a budget
_set_at: WeakKeyDictionary[psycopg.Connection[Any], float] = WeakKeyDictionary()


class BudgetCursor(psycopg.Cursor[DictRow]):
    """A cursor that gives each statement what is left of the call's budget."""

    def _spend(self) -> None:
        budget = current.get()
        if budget is None:
            return
        left = budget.remaining()
        if left <= 0:
            raise ToolBudgetSpent("the tool call's time for database work is used up")
        conn = self.connection
        status = conn.info.transaction_status
        if status == pq.TransactionStatus.INERROR:
            return  # nothing can be set; the statement fails on its own
        now = time.monotonic()
        last = _set_at.get(conn)
        if (
            status == pq.TransactionStatus.IDLE
            or last is None
            or now - last > REFRESH_SECONDS
            or left < REFRESH_SECONDS
        ):
            # Rounded up and never 0 (0 would mean "no limit").
            ms = max(1, math.ceil(min(left, STATEMENT_CAP_SECONDS) * 1000))
            super().execute("SELECT set_config('statement_timeout', %s, true)", (f"{ms}ms",))
            _set_at[conn] = now

    # Arguments are passed through as given: psycopg's own signatures (overloads
    # for template strings) stay the single definition of what is accepted.
    def execute(self, *args: Any, **kwargs: Any) -> Self:
        self._spend()
        return super().execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> None:
        self._spend()
        super().executemany(*args, **kwargs)


@contextmanager
def budgeted(conn: psycopg.Connection[DictRow]) -> Iterator[psycopg.Connection[DictRow]]:
    """Statements on ``conn`` are held to the running tool call's budget inside the
    block. The connection's own ``statement_timeout`` is back once the transaction
    ends (the caller's commit or rollback)."""
    before = conn.cursor_factory
    conn.cursor_factory = BudgetCursor
    try:
        yield conn
    finally:
        conn.cursor_factory = before
        _set_at.pop(conn, None)
