"""The stored counts for the console Overview and the coverage block (migration
0038): ``ci.ops_counts`` holds the answers of the six counting views, recounted by
``ci.refresh_ops_counts`` after a run. Without them the live views answer."""

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import set_mode
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.redaction import FakeRedactor
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval

MIGRATION = Path(__file__).resolve().parents[2] / "db/migrations/0038_ops_counts.sql"
SECTIONS = ("versions", "failures", "labels", "uncertain_labels", "extraction", "originals")
LIVE_VIEWS = (
    "ops_version_states",
    "ops_failures",
    "ops_label_counts",
    "ops_extraction_counts",
    "ops_original_counts",
    "coverage_counts",
)
Stack = Callable[[AppRole | None], Connection]


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[Stack]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(
        connector,
        graph,
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    counts = process_all(
        lambda: connector("ci_ingest"),
        Backends(
            FakeScreener(check_pii=False),
            FakeClassifier(),
            FakeEmbedder(),
            graph=graph,
            extraction_enabled=True,
            redactor=FakeRedactor(),
        ),
        workers=1,
    )
    assert counts["index"] > 0 and counts["failed"] == 0
    yield connector


@pytest.fixture(autouse=True)
def no_cached_coverage() -> Iterator[None]:
    """The coverage cache is per process and keyed by database name, and every test
    module reuses that name: nothing read here may be left for another module."""
    retrieval._coverage_cache.clear()
    yield
    retrieval._coverage_cache.clear()


def sections(overview: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The six stored sections, in an order that does not depend on ties."""
    return {
        name: sorted(overview[name], key=lambda row: sorted(row.items(), key=str))
        for name in SECTIONS
    }


def descending(rows: list[dict[str, Any]], count: str) -> bool:
    values = [row[count] for row in rows]
    return values == sorted(values, reverse=True)


def refresh(conn: Connection, min_age: str | None = None, *, changed: bool = False) -> bool:
    """``changed`` stands for work that records no run (the CLI): here, the changes
    the tests make by hand."""
    row = (
        conn.execute("SELECT refresh_ops_counts(changed => %s) AS done", (changed,))
        if min_age is None
        else conn.execute("SELECT refresh_ops_counts(%s::interval, %s) AS done", (min_age, changed))
    ).fetchone()
    assert row is not None
    return bool(row["done"])


def aged(conn: Connection) -> None:
    """Make the stored counts an hour old (as the owner): refresh_ops_counts
    never recounts counts younger than a minute, whatever min_age it is given."""
    conn.execute("UPDATE ops_counts SET refreshed_at = refreshed_at - interval '1 hour'")


def watched(conn: Connection) -> list[str]:
    """Every statement sent on ``conn`` from now on."""
    seen: list[str] = []

    class Watching(psycopg.Cursor[Any]):
        def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(query))
            return super().execute(query, *args, **kwargs)

    conn.cursor_factory = Watching
    return seen


def test_without_stored_counts_the_live_views_answer(stack: Stack) -> None:
    """AC-3.1: the migration fills nothing, so the answers are today's; the same
    holds on a database that does not have the migration."""
    with stack("ci_operator") as conn:
        empty = ops.overview(conn)
        conn.rollback()
    assert empty["counts_as_of"] is None
    assert {v["processing_state"] for v in empty["versions"]} >= {"published"}
    assert any(row["decision"] == "yes" for row in empty["labels"])
    with stack("ci_search") as conn:
        live = retrieval.coverage(conn, max_age=0)
        conn.rollback()
    assert live["published"] > 0

    with stack(None) as conn:  # without migration 0038, undone by the rollback
        conn.execute("DROP VIEW coverage_snapshot")
        conn.execute("DROP FUNCTION refresh_ops_counts(interval, boolean)")
        conn.execute("DROP TABLE ops_counts")
        assert ops.refresh_counts(conn) is False
        conn.execute("SET LOCAL ROLE ci_operator")
        absent = ops.overview(conn)
        conn.execute("SET LOCAL ROLE ci_search")
        assert retrieval.coverage(conn, max_age=0) == live
        conn.rollback()
    assert absent == empty


def test_only_ingestion_refreshes_and_search_reads_coverage_only(stack: Stack) -> None:
    """AC-3.5, and that ingestion cannot read the table: each role gets the one
    thing it needs."""

    def refused(role: AppRole, statement: str) -> bool:
        with stack(role) as conn:
            try:
                conn.execute(statement)
            except InsufficientPrivilege:
                return True
            finally:
                conn.rollback()
        return False

    assert refused("ci_search", "SELECT * FROM ops_counts")
    assert refused("ci_search", "SELECT refresh_ops_counts()")
    assert not refused("ci_search", "SELECT rows, refreshed_at FROM coverage_snapshot")
    assert refused("ci_operator", "SELECT refresh_ops_counts()")
    assert not refused("ci_operator", "SELECT * FROM ops_counts")
    assert refused("ci_ingest", "SELECT * FROM ops_counts")
    assert refused("ci_ingest", "INSERT INTO ops_counts VALUES ('versions', '[]', NULL, now())")
    assert refused("ci_ingest", "SELECT * FROM coverage_snapshot")
    assert refused("ci_operator", "DELETE FROM ops_counts")


def test_after_a_refresh_the_overview_reads_the_stored_counts(stack: Stack) -> None:
    """AC-3.2, AC-3.3 (overview) and the empty-section case: the six sections equal
    the live answer, come from one read of the table, and say when they were taken."""
    with stack("ci_operator") as conn:
        live = ops.overview(conn)
        conn.rollback()
    with stack("ci_ingest") as conn:
        assert ops.refresh_counts(conn) is True
        assert ops.refresh_counts(conn) is False  # under five minutes old
    with stack("ci_operator") as conn:
        seen = watched(conn)
        stored = ops.overview(conn)
        conn.rollback()
    assert sections(stored) == sections(live)
    assert stored["counts_as_of"] is not None
    assert datetime.fromisoformat(stored["counts_as_of"]).tzinfo is not None
    # The fixtures have no failed message: an empty section is still a stored answer.
    assert stored["failures"] == [] and live["failures"] == []
    assert descending(stored["versions"], "versions")
    assert descending(stored["labels"], "messages") and stored["labels"]
    assert {row["decision"] for row in stored["labels"]} == {"yes"}
    assert {row["decision"] for row in stored["uncertain_labels"]} <= {"review", "unresolved"}
    assert descending(stored["extraction"], "attachments") and stored["extraction"]
    assert descending(stored["originals"], "attachments") and stored["originals"]
    for name in ("mailboxes", "backlog", "email_range"):  # always live
        assert stored[name] == live[name]
    assert not [s for s in seen for view in LIVE_VIEWS if view in s], seen
    assert sum("FROM ops_counts" in s for s in seen) == 1


def test_coverage_reads_the_stored_counts_and_keeps_them_a_minute(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3.6, AC-3.3 (coverage) and the cache rule: the stored fold equals the
    live one, and a stored answer is read again after a minute, not thirty."""
    with stack("ci_search") as conn:
        with monkeypatch.context() as patch:
            patch.setattr(retrieval, "_stored_coverage", lambda conn: None)
            live = retrieval.coverage(conn, max_age=0)
        seen = watched(conn)
        stored = retrieval.coverage(conn, max_age=0)
        assert stored == live and live["published"] > 0
        assert not [s for s in seen for view in LIVE_VIEWS if view in s], seen
        assert sum("FROM coverage_snapshot" in s for s in seen) == 1
        seen.clear()
        assert retrieval.coverage(conn) == stored and seen == []  # inside the minute
        monkeypatch.setattr(retrieval, "COVERAGE_STORED_SECONDS", 0.0)
        assert retrieval.coverage(conn) == stored
        assert sum("FROM coverage_snapshot" in s for s in seen) == 1  # read again
        conn.rollback()


def test_a_change_shows_after_the_next_refresh(stack: Stack) -> None:
    """AC-3.4: stored counts do not move until they are recounted, and a recount
    inside min_age is skipped."""
    with stack(None) as conn:
        aged(conn)
        assert refresh(conn, "0", changed=True) is True
        assert refresh(conn, "0", changed=True) is False  # min_age is at least a minute
        before = ops.overview(conn)
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'failed',"
            " last_error_code = 'pytest_error' WHERE id = (SELECT id FROM communication_versions"
            " WHERE processing_state = 'published' LIMIT 1)"
        )
        assert ops.overview(conn) == before  # not shown yet
        assert refresh(conn) is False  # younger than five minutes
        assert refresh(conn, "1 hour", changed=True) is False
        assert ops.overview(conn) == before
        aged(conn)
        assert refresh(conn) is False  # old enough, but no run finished since
        assert ops.overview(conn)["failures"] == before["failures"]
        assert refresh(conn, changed=True) is True
        after = ops.overview(conn)
        conn.rollback()
    assert before["failures"] == []
    assert after["failures"] == [{"error_code": "pytest_error", "versions": 1}]
    assert sum(v["versions"] for v in after["versions"]) == sum(
        v["versions"] for v in before["versions"]
    )


def test_a_refresh_does_not_wait_for_another_one(stack: Stack) -> None:
    """Two runs ending together: the second returns at once instead of queueing
    behind the first one's recount."""
    with stack(None) as holder, stack("ci_ingest") as conn:
        aged(holder)
        holder.commit()
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('ci.refresh_ops_counts'))")
        conn.execute("SET LOCAL statement_timeout = '5s'")  # waiting would be an error
        assert refresh(conn, "0") is False
        conn.rollback()
        holder.rollback()
        assert refresh(conn, "0", changed=True) is True
        conn.rollback()


def test_a_mode_change_sets_the_stored_coverage_aside(stack: Stack) -> None:
    """Coverage counts are for the active dataset; the other five are not. After a
    mode change the stored coverage is not used, and the next refresh recounts even
    when the counts are fresh."""
    with stack(None) as conn:
        aged(conn)
        assert refresh(conn, "0", changed=True) is True
        assert retrieval.coverage(conn, max_age=0)["published"] > 0
        set_mode(conn, "paused", "pytest")
        assert conn.execute("SELECT 1 FROM coverage_snapshot").fetchone() is None
        seen = watched(conn)
        assert retrieval.coverage(conn, max_age=0) == {}  # live: paused has no dataset
        assert any("coverage_counts" in s for s in seen)
        assert ops.overview(conn)["counts_as_of"] is not None  # the ops counts still hold
        assert refresh(conn) is True  # min_age is ignored: the dataset changed
        assert refresh(conn) is False
        row = conn.execute("SELECT rows FROM coverage_snapshot").fetchone()
        assert row == {"rows": []}
        conn.rollback()


def test_the_migration_applies_without_superuser(stack: Stack) -> None:
    """AC-3.8: Azure gives the migrating login no superuser. The function runs as
    that login, which must be able to read the six counting views."""
    with stack(None) as conn:
        conn.execute("DROP VIEW coverage_snapshot")
        conn.execute("DROP FUNCTION refresh_ops_counts(interval, boolean)")
        conn.execute("DROP TABLE ops_counts")
        conn.execute("CREATE ROLE pytest_migrator_38 NOSUPERUSER")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA ci TO pytest_migrator_38")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ci TO pytest_migrator_38")
        conn.execute("SET ROLE pytest_migrator_38")
        conn.execute(MIGRATION.read_text())
        conn.execute("SET ROLE ci_ingest")
        assert refresh(conn) is True
        conn.execute("SET ROLE ci_operator")
        overview = ops.overview(conn)
        conn.execute("SET ROLE ci_search")
        row = conn.execute("SELECT jsonb_array_length(rows) AS n FROM coverage_snapshot").fetchone()
        conn.rollback()
    assert overview["counts_as_of"] is not None and overview["versions"]
    assert row is not None and row["n"] > 0


def test_counts_follow_the_last_run_of_a_busy_period(stack: Stack) -> None:
    """A run that finished after the last recount makes the next call
    recount once the counts are min_age old, even when that call follows no work;
    with no run finished since, a call counts nothing, however old the counts."""

    def counted_at(conn: Connection) -> datetime:
        row = conn.execute("SELECT min(refreshed_at) AS t FROM ops_counts").fetchone()
        assert row is not None
        return cast(datetime, row["t"])

    with stack(None) as conn:
        aged(conn)
        assert refresh(conn, "0", changed=True) is True
        conn.execute("UPDATE ops_counts SET refreshed_at = now() - interval '10 minutes'")
        conn.execute(
            "UPDATE ops_runs SET finished_at = now() - interval '1 hour'"
            " WHERE finished_at > now() - interval '10 minutes'"
        )
        before = counted_at(conn)
        assert refresh(conn) is False  # ten minutes old, but nothing finished since
        assert counted_at(conn) == before
        # The last run of a busy period ended after the last recount.
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'failed',"
            " last_error_code = 'pytest_error' WHERE id = (SELECT id FROM communication_versions"
            " WHERE processing_state = 'published' LIMIT 1)"
        )
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, started_at, finished_at, status)"
            " VALUES ('process', 'schedule', now() - interval '3 minutes',"
            " now() - interval '2 minutes', 'succeeded')"
        )
        conn.execute("UPDATE ops_counts SET refreshed_at = now() - interval '4 minutes'")
        assert refresh(conn) is False  # the run finished after, but under 5 minutes old
        assert ops.overview(conn)["failures"] == []
        assert refresh(conn, "1 minute") is True  # old enough: recounted
        assert ops.overview(conn)["failures"] == [{"error_code": "pytest_error", "versions": 1}]
        conn.execute("UPDATE ops_counts SET refreshed_at = now() - interval '90 seconds'")
        assert refresh(conn, "1 minute") is False  # the run was before this recount
        # A run still going (no finished_at) is not a change yet.
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, started_at) VALUES ('process', 'schedule', now())"
        )
        assert refresh(conn, "1 minute") is False
        conn.rollback()


def test_a_refresh_has_a_time_limit_of_its_own(stack: Stack) -> None:
    """The recount runs under statement_timeout = 3min, set for its own
    transaction only."""
    with stack("ci_ingest") as conn:
        before = conn.execute("SHOW statement_timeout").fetchone()
        conn.commit()
        seen = watched(conn)
        ops.refresh_counts(conn, changed=True)
        after = conn.execute("SHOW statement_timeout").fetchone()
        conn.rollback()
    limit = [i for i, s in enumerate(seen) if "statement_timeout" in s]
    call = [i for i, s in enumerate(seen) if "refresh_ops_counts(changed" in s]
    assert limit and call and limit[0] < call[0], seen
    assert "SET LOCAL statement_timeout = '3min'" in seen[limit[0]]
    assert before == after


def test_coverage_snapshot_shows_nothing_but_the_coverage_row(stack: Stack) -> None:
    """coverage_snapshot is a security_barrier view: whatever conditions
    ci_search adds, including a cheap function of its own that reports each row
    it is called on, only the coverage row is evaluated and returned."""
    told: list[str] = []
    with stack(None) as conn:
        aged(conn)
        assert refresh(conn, "0", changed=True) is True
        options = conn.execute(
            "SELECT reloptions FROM pg_class WHERE oid = 'ci.coverage_snapshot'::regclass"
        ).fetchone()
        coverage = conn.execute("SELECT rows FROM ops_counts WHERE name = 'coverage'").fetchone()
        conn.execute("SET LOCAL ROLE ci_search")
        seen = conn.execute(
            "SELECT rows FROM coverage_snapshot WHERE jsonb_typeof(rows) = 'array'"
            " OR rows::text LIKE '%versions%'"
        ).fetchall()
        conn.execute(
            "CREATE FUNCTION pg_temp.called(seen text) RETURNS boolean LANGUAGE plpgsql"
            " COST 0.0000001 AS $$ BEGIN RAISE NOTICE 'called %', seen; RETURN true; END $$"
        )
        conn.add_notice_handler(lambda d: told.append(d.message_primary or ""))
        filtered = conn.execute(
            "SELECT rows FROM coverage_snapshot WHERE pg_temp.called(rows::text)"
        ).fetchall()
        conn.rollback()
    assert options is not None and "security_barrier=true" in (options["reloptions"] or [])
    assert coverage is not None and seen == filtered == [{"rows": coverage["rows"]}]
    assert len([t for t in told if t.startswith("called ")]) == 1, told


def test_the_functions_put_temporary_objects_last(stack: Stack) -> None:
    """The SECURITY DEFINER functions of 0038 and 0039 set search_path ending in
    pg_temp."""
    with stack(None) as conn:
        rows = conn.execute(
            "SELECT proname, proconfig FROM pg_proc WHERE pronamespace = 'ci'::regnamespace"
            " AND proname IN ('refresh_ops_counts', 'published_page')"
        ).fetchall()
        conn.rollback()
    assert {r["proname"] for r in rows} == {"refresh_ops_counts", "published_page"}
    for r in rows:
        assert "search_path=ci, public, pg_temp" in r["proconfig"], r


def test_ingestion_cannot_force_recounts_back_to_back(stack: Stack) -> None:
    """The recount function keeps a minimum interval of one minute between
    recounts, whatever min_age the ingestion role passes."""
    with stack(None) as owner:
        aged(owner)
        owner.commit()
    with stack("ci_ingest") as conn:
        assert ops.refresh_counts(conn, changed=True) is True
        assert refresh(conn, "0", changed=True) is False
        assert refresh(conn, "-1 hour", changed=True) is False
        conn.rollback()


def test_a_refresh_waits_at_most_five_seconds_for_a_lock(stack: Stack) -> None:
    """Behind a lock (a migration, a long write) the recount gives up
    after 5 s (lock_timeout on the function), and the run that called it goes on."""
    with stack(None) as holder, stack("ci_ingest") as conn:
        aged(holder)
        holder.commit()
        holder.execute("LOCK TABLE ci.ops_counts IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        assert ops.refresh_counts(conn, changed=True) is False
        waited = time.monotonic() - started
        holder.rollback()
        conn.rollback()
    assert 4.5 < waited < 15, waited
