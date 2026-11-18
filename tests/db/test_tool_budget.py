"""One MCP tool call's database work has a time budget (ci_retrieval.budget):
each statement gets what is left of it, and a spent budget stops the next
statement before it reaches the database. Nothing stays on the connection."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.errors import QueryCanceled

from ci_core.db import Connection, connect
from ci_retrieval import budget, mcp_server


@pytest.fixture
def search(database: str) -> Iterator[Connection]:
    """A connection as the search role, with the tools' session limit of 40 s."""
    with connect(dbname=database, role="ci_search") as conn:
        conn.execute("SELECT set_config('statement_timeout', '40s', false)")
        conn.commit()
        yield conn
        conn.rollback()


def timeout(conn: Connection) -> str:
    row = conn.execute("SHOW statement_timeout").fetchone()
    assert row is not None
    return str(row["statement_timeout"])


@contextmanager
def a_budget(seconds: float) -> Iterator[None]:
    token = budget.current.set(budget.Budget(seconds))
    try:
        yield
    finally:
        budget.current.reset(token)


def test_statements_share_one_budget_and_leave_nothing_behind(search: Connection) -> None:
    """AC-1.3 (the statements) and AC-1.4: two statements of 0.6 s in a budget of
    1 s. The second is stopped at about 1 s in all, a third never reaches the
    database, and the connection is as it was."""
    before, factory = timeout(search), search.cursor_factory
    search.commit()
    assert before == "40s"
    with a_budget(1.0), budget.budgeted(search):
        assert search.cursor_factory is budget.BudgetCursor
        started = time.monotonic()
        search.execute("SELECT pg_sleep(0.6)")
        assert 0 < int(timeout(search).removesuffix("ms")) <= 1000  # what was left
        with pytest.raises(QueryCanceled):
            search.execute("SELECT pg_sleep(0.6)")
        took = time.monotonic() - started
        # The upper bounds here are loose on purpose (budget + 2 s): a slow runner
        # must not fail them. QueryCanceled is what proves the statement was stopped.
        assert 0.9 <= took < 3.0
        search.rollback()
        sent: list[Any] = []
        search.add_notice_handler(sent.append)
        with pytest.raises(budget.ToolBudgetSpent):
            search.execute("DO $$ BEGIN RAISE NOTICE 'reached the database'; END $$")
        with pytest.raises(budget.ToolBudgetSpent):
            search.cursor().executemany("SELECT %s", [(1,), (2,)])
        assert sent == []
        assert search.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert search.cursor_factory is factory
    assert timeout(search) == before
    search.commit()
    # Outside a tool call (no budget) the same cursor adds nothing.
    with budget.budgeted(search):
        search.execute("SELECT 1")
        assert timeout(search) == before


def test_no_statement_gets_longer_than_the_cap(
    search: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However much budget is left, one statement gets at most the cap (40 s, the
    limit tools had before the budget); with less left, it gets what is left."""
    monkeypatch.setattr(budget, "STATEMENT_CAP_SECONDS", 7.0)
    with a_budget(3600.0), budget.budgeted(search):
        search.execute("SELECT 1")
        assert timeout(search) == "7s"  # the cap, not the hour that is left
        search.rollback()
    with a_budget(2.5), budget.budgeted(search):
        search.execute("SELECT 1")
        assert 0 < int(timeout(search).removesuffix("ms")) <= 2500  # under the cap
        search.rollback()
    assert timeout(search) == "40s"


def test_the_budget_holds_in_separate_transactions_and_inside_a_long_one(
    search: Connection,
) -> None:
    """Each transaction starts with what is left (tools open a connection more than
    once, and _attach_resolutions rolls back), and a long transaction is given the
    smaller limit again after a second."""
    with a_budget(1.0), budget.budgeted(search):
        started = time.monotonic()
        search.execute("SELECT pg_sleep(0.6)")
        search.commit()
        with pytest.raises(QueryCanceled):
            search.execute("SELECT pg_sleep(0.6)")
        assert 0.9 <= time.monotonic() - started < 3.0
        search.rollback()
    with a_budget(2.5), budget.budgeted(search):
        started = time.monotonic()
        search.execute("SELECT pg_sleep(1.2)")  # limit set to 2.5 s at the start
        with pytest.raises(QueryCanceled):
            search.execute("SELECT pg_sleep(2)")  # set again: 1.3 s are left
        assert 2.3 <= time.monotonic() - started < 4.5
        search.rollback()
    assert timeout(search) == "40s"


def test_a_tool_whose_statements_use_up_the_budget_answers_too_slow(
    database: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-1.3 (the answer): a guarded tool on the connector build_server gives its
    tools. Its statements are stopped by the budget and the agent is told why."""
    monkeypatch.setattr(mcp_server, "TOOL_BUDGET_SECONDS", 1.0)
    lent: list[Connection] = []

    def lend() -> AbstractContextManager[Connection]:
        conn = connect(dbname=database, role="ci_search")
        lent.append(conn)
        return conn

    connector = mcp_server._within_budget(lend)

    def slow_statements(first: float, second: float) -> dict[str, Any]:
        with connector() as conn:
            conn.execute("SELECT pg_sleep(%s)", (first,))
            conn.execute("SELECT pg_sleep(%s)", (second,))
        return {"done": True}

    tool = mcp_server._guarded(slow_statements)

    async def scenario() -> tuple[Any, float, Any]:
        started = time.monotonic()
        stopped = await tool(0.6, 0.6)
        took = time.monotonic() - started
        return stopped, took, await tool(0.1, 0.1)

    with caplog.at_level("INFO", logger="ci_retrieval.mcp_server"):
        stopped, took, fine = asyncio.run(scenario())
    assert stopped == mcp_server.TOO_SLOW and 0.9 <= took < 3.0
    assert fine == {"done": True}
    assert [r.getMessage().split(" in ")[0] for r in caplog.records] == [
        "mcp tool slow_statements too_slow",
        "mcp tool slow_statements ok",
    ]
    assert len(lent) == 2 and all(c.closed for c in lent)
