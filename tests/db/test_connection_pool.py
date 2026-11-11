"""Connections kept open and reused by the console and the search app
(ci_core.db.ConnectionPool): work is committed or rolled back as with a plain
connection, a connection goes back only when it is clean, and old, idle-and-dead
or surplus connections are closed instead of reused."""

import threading
from collections.abc import Iterator

import psycopg
import pytest

from ci_core import db
from ci_core.db import AppRole, Connection, ConnectionPool, connect


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def scratch(database: str) -> Iterator[str]:
    with connect(dbname=database) as c:
        c.execute("CREATE TABLE IF NOT EXISTS pool_check (n int)")
        c.execute("TRUNCATE pool_check")
    yield database


def count_rows(database: str) -> int:
    with connect(dbname=database) as c:
        row = c.execute("SELECT count(*) AS n FROM pool_check").fetchone()
        assert row is not None
        return int(row["n"])


def admin_pool(database: str, **options: object) -> ConnectionPool:
    """A pool of administrator connections (the role is only the pool's key here)."""
    return ConnectionPool(lambda role: connect(dbname=database), **options)  # type: ignore[arg-type]


def test_one_connection_serves_requests_one_after_another(database: str) -> None:
    pool = ConnectionPool(lambda role: connect(dbname=database, role=role))
    pids = set()
    for _ in range(5):
        with pool.connection("ci_search") as conn:
            row = conn.execute(
                "SELECT pg_backend_pid() AS pid, current_user AS who,"
                " current_setting('statement_timeout') AS limit_"
            ).fetchone()
            assert row is not None
            pids.add(row["pid"])
            # The role and its timeouts carry over to the next request.
            assert (row["who"], row["limit_"]) == ("ci_search", "2min")
    assert len(pids) == 1 and pool.opened == 1
    # Roles never share a connection.
    with pool.connection("ci_operator") as conn:
        row = conn.execute("SELECT current_user AS who").fetchone()
        assert row is not None and row["who"] == "ci_operator"
    assert pool.opened == 2
    pool.close()


def test_work_is_committed_or_rolled_back_like_a_plain_connection(scratch: str) -> None:
    pool = admin_pool(scratch)
    with pool.connection("ci_operator") as conn:
        conn.execute("INSERT INTO pool_check VALUES (1)")
    assert count_rows(scratch) == 1
    with pytest.raises(RuntimeError), pool.connection("ci_operator") as conn:
        conn.execute("INSERT INTO pool_check VALUES (2)")
        raise RuntimeError("the request failed")
    assert count_rows(scratch) == 1
    # A failed statement leaves the connection clean for the next request.
    with pytest.raises(psycopg.errors.UndefinedTable), pool.connection("ci_operator") as conn:
        conn.execute("SELECT * FROM no_such_table")
    with pool.connection("ci_operator") as conn:
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        conn.execute("INSERT INTO pool_check VALUES (3)")
    assert count_rows(scratch) == 2
    assert pool.opened == 1
    pool.close()


def test_old_connections_are_replaced(database: str) -> None:
    clock = Clock()
    pool = admin_pool(database, max_age=1800, clock=clock)
    with pool.connection("ci_search"):
        pass
    clock.now += 1801
    with pool.connection("ci_search"):
        pass
    assert pool.opened == 2 and pool.idle_count("ci_search") == 1
    pool.close()


def test_a_connection_the_server_dropped_is_not_lent(database: str) -> None:
    clock = Clock()
    pool = admin_pool(database, ping_after=60, clock=clock)
    with pool.connection("ci_search") as conn:
        row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
        assert row is not None
        pid = row["pid"]
    with connect(dbname=database) as killer:
        killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
    clock.now += 61  # idle long enough to be checked first
    with pool.connection("ci_search") as conn:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        assert row is not None and row["ok"] == 1
    assert pool.opened == 2
    pool.close()


def test_size_zero_turns_pooling_off(database: str) -> None:
    pool = admin_pool(database, max_idle=0)
    kept: list[Connection] = []
    for _ in range(3):
        with pool.connection("ci_search") as conn:
            kept.append(conn)
    assert pool.opened == 3 and all(c.closed for c in kept)
    assert pool.idle_count("ci_search") == 0


def test_only_max_idle_connections_are_kept(database: str) -> None:
    pool = admin_pool(database, max_idle=1)
    with pool.connection("ci_search") as first, pool.connection("ci_search") as second:
        assert first is not second
    assert pool.opened == 2 and pool.idle_count("ci_search") == 1
    assert first.closed != second.closed  # one kept, one closed
    pool.close()


def test_requests_in_parallel_threads(database: str) -> None:
    pool = admin_pool(database, max_idle=4)
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(15):
                with pool.connection("ci_search") as conn:
                    conn.execute("SELECT pg_sleep(0.001)")
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert pool.opened <= 6 and pool.idle_count("ci_search") <= 4
    pool.close()


def test_pool_size_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    assert db.shared_pool().max_idle == 0
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("DB_POOL_SIZE", "not a number")
    assert db.shared_pool().max_idle == 4
    monkeypatch.setattr(db, "_pool", None)
    role: AppRole = "ci_search"
    assert db.pooled(role) is not None  # a context manager; nothing opened yet
    monkeypatch.setattr(db, "_pool", None)
