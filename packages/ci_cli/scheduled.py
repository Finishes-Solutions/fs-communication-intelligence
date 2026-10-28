"""Scheduled jobs run by the ingestion Function App (apps/ingestion).

Two switches must both be on for anything to run: the app setting
(``CAPTURE_ENABLED`` / ``PROCESSING_ENABLED``, a hard kill switch in Azure) and
the schedule in ``ci.ops_settings`` (changed from the admin console or
``ci schedule``). The database mode still applies: paused or empty scope means
nothing is captured or published. Every capture and every processing pass that
did work is recorded in ``ci.ops_runs`` with counts and codes only.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

import psycopg

from ci_cli import backends
from ci_cli.pipeline import capture_profile, connection_and_corpus
from ci_core import ops
from ci_core.db import AppRole, Connection, connect
from ci_core.runtime import EmptyScopeError, RuntimeConfigError
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.inventory import sync_inventory
from ci_email.process import process_all, record_usage, store_missing_originals

log = logging.getLogger("ci.scheduled")
# A processing run still 'running' this long after its time budget was stopped
# without recording its end (restart, deploy, host time limit).
STALE_RUN_GRACE_MINUTES = 60


def _connect(role: AppRole | None) -> Connection:
    return connect(role=role or "ci_ingest")


def capture_now(settings: Settings, hours: float) -> tuple[str, dict[str, int]]:
    """Inventory sync, then capture the last ``hours`` from every eligible mailbox.

    Returns a run status (``succeeded``, ``partial``, ``refused``) and counts.
    """
    graph = backends.graph_client(settings)
    with connect(role="ci_ingest") as conn:
        connection, corpus = connection_and_corpus(conn, settings)
        states = sync_inventory(conn, graph, connection, corpus, "ingestion-function").states
        conn.commit()
    log.info("inventory states=%s", states)
    total: Counter[str] = Counter({f"mailboxes_{k}": v for k, v in states.items()})
    try:
        results = capture_window(
            _connect,
            graph,
            hours=hours,
            include_sent=settings.email_include_sent,
            profile=capture_profile(settings),
            workers=settings.capture_workers,
        )
    except (EmptyScopeError, RuntimeConfigError) as exc:
        log.warning("capture refused: %s", type(exc).__name__)
        total["refused_" + type(exc).__name__] += 1
        return "refused", dict(total)
    for r in results:
        total.update(r.counts)
        if r.error:
            total["mailbox_errors"] += 1
            log.warning("capture error code=%s", r.error)
    log.info("capture mailboxes=%d counts=%s", len(results), dict(total))
    return ("partial" if total["mailbox_errors"] else "succeeded"), dict(total)


def run_capture(settings: Settings) -> dict[str, int] | None:
    """Capture ``CAPTURE_HOURS`` now, ignoring the schedule (CLI and tests)."""
    if not settings.capture_enabled:
        log.info("capture skipped: CAPTURE_ENABLED is false")
        return None
    status, counts = capture_now(settings, settings.capture_hours)
    return None if status == "refused" else counts


def scheduler_tick(settings: Settings, now: datetime | None = None) -> ops.Decision:
    """Run a capture if the schedule (or a "run now" request) says one is due."""
    now = now or datetime.now(UTC)
    if not settings.capture_enabled:
        return ops.Decision(False, "CAPTURE_ENABLED app setting is false")
    with connect(role="ci_ingest") as conn:
        decision = ops.capture_decision(ops.read_schedule(conn), now)
        if not decision.due:
            return decision
        if ops.capture_in_progress(conn, now):
            return ops.Decision(False, "a capture is still running")
        if ops.close_stale_runs(conn, "capture", ops.RUNNING_CAPTURE_GRACE, now):
            log.warning("closed stale capture run(s)")
        run_id = ops.begin_capture(conn, decision, now)
        conn.commit()
    try:
        status, counts = capture_now(settings, decision.hours)
        error = None
    except Exception as exc:  # recorded as a code; the next tick tries again
        status, counts, error = "failed", {}, type(exc).__name__[:60]
        log.exception("capture failed")
    with connect(role="ci_ingest") as conn:
        ops.finish_run(conn, run_id, status, counts, error)
    return decision


def run_processing(settings: Settings) -> dict[str, int] | None:
    if not settings.processing_enabled:
        log.info("processing skipped: PROCESSING_ENABLED is false")
        return None
    with connect(role="ci_ingest") as conn:
        schedule = ops.read_schedule(conn)
        if not schedule.processing_enabled:
            log.info("processing skipped: turned off in the schedule")
            return None
        stale = ops.close_stale_runs(
            conn,
            "process",
            timedelta(minutes=settings.processing_minutes + STALE_RUN_GRACE_MINUTES),
            datetime.now(UTC),
        )
        if stale:
            log.warning("closed %s stale processing run(s)", stale)
        run_id = ops.start_run(conn, "process", "schedule")
        conn.commit()

    def progress(counts: dict[str, int]) -> None:
        log.info("processing progress: %s", counts)
        with connect(role="ci_ingest") as pconn:
            ops.update_run_counts(pconn, run_id, counts)

    status, error = "succeeded", None
    configured = None
    try:
        configured = backends.with_switches(
            backends.processing_backends(backends.with_model(settings, schedule)),
            settings,
            schedule,
        )
        counts = dict(
            process_all(
                lambda: connect(role="ci_ingest"),
                configured,
                workers=settings.processing_workers,
                max_seconds=settings.processing_minutes * 60,
                progress=progress,
            )
        )
        counts.update(store_missing_originals(lambda: connect(role="ci_ingest"), configured))
    except Exception as exc:
        counts, status, error = {}, "failed", type(exc).__name__[:60]
        log.exception("processing failed")
    with connect(role="ci_ingest") as conn:
        if configured is not None and configured.meter is not None:
            # What the run's paid calls used, kept even when the run failed part-way.
            # A savepoint: a database without migration 0013 must not lose the run record.
            try:
                with conn.transaction():
                    record_usage(conn, configured.meter.drain(), source="processing", run_id=run_id)
            except psycopg.Error as exc:
                log.warning("usage not recorded: %s", type(exc).__name__)
        if counts or error:
            if counts.get("failed"):
                status = "partial"
            ops.finish_run(conn, run_id, status, counts, error)
        else:  # nothing was queued: keep the history to runs that did work
            conn.execute("DELETE FROM ops_runs WHERE id = %s", (run_id,))
    log.info("processing counts=%s", counts)
    return counts
