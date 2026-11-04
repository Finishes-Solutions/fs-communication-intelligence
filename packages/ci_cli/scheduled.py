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
import time
from collections import Counter
from collections.abc import Callable
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
from ci_email.resolutions import resolve_pending_actions
from ci_email.risk_ratings import rate_pending_risks
from ci_fireflies import capture as ff_capture
from ci_fireflies.inventory import connection_and_corpus as ff_connection
from ci_fireflies.inventory import sync_users

log = logging.getLogger("ci.scheduled")
# A processing run still 'running' this long after its time budget was stopped
# without recording its end (restart, deploy, host time limit).
STALE_RUN_GRACE_MINUTES = 60
# Rating risk emails after processing (ci_email.risk_ratings): at most this long per run.
RISK_RATING_SECONDS = 120.0
# Checking action items with new replies (ci_email.resolutions): at most this long per run.
RESOLUTION_SECONDS = 180.0
# One tick works through backfill windows for at most this long before returning
# (the host stops a function after 2 hours; a window is not started past this).
BACKFILL_TICK_SECONDS = 100 * 60


def _connect(role: AppRole | None) -> Connection:
    return connect(role=role or "ci_ingest")


def capture_now(
    settings: Settings,
    hours: float | None,
    now: datetime | None = None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    sync: bool = True,
) -> tuple[str, dict[str, int]]:
    """Inventory sync (unless ``sync`` is false), then capture the ``hours`` before
    ``now`` (default: the current time), or the explicit ``since``/``until`` window,
    from every eligible mailbox.

    Returns a run status (``succeeded``, ``partial``, ``refused``) and counts.
    """
    graph = backends.graph_client(settings)
    total: Counter[str] = Counter()
    if sync:
        with connect(role="ci_ingest") as conn:
            connection, corpus = connection_and_corpus(conn, settings)
            states = sync_inventory(conn, graph, connection, corpus, "ingestion-function").states
            conn.commit()
        log.info("inventory states=%s", states)
        total.update({f"mailboxes_{k}": v for k, v in states.items()})
    try:
        results = capture_window(
            _connect,
            graph,
            hours=hours,
            include_sent=settings.email_include_sent,
            profile=capture_profile(settings),
            workers=settings.capture_workers,
            now=now,
            since=since,
            until=until,
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


def fireflies_capture_now(
    settings: Settings,
    *,
    since: datetime,
    until: datetime,
    sync: bool = True,
) -> tuple[str, dict[str, int]]:
    """Team inventory (unless ``sync`` is false), then capture the meetings of the window.
    Returns a run status (``succeeded``, ``partial``, ``refused``) and counts."""
    client = backends.fireflies_client(settings)
    total: Counter[str] = Counter()
    if sync:
        with connect(role="ci_ingest") as conn:
            connection, corpus = ff_connection(conn)
            states = sync_users(conn, client, connection, corpus, "ingestion-function").states
            conn.commit()
        total.update({f"users_{k}": v for k, v in states.items()})
    try:
        result = ff_capture.capture_window(
            _connect, client, since=since, until=until, profile=capture_profile(settings)
        )
    except (EmptyScopeError, RuntimeConfigError) as exc:
        log.warning("fireflies capture refused: %s", type(exc).__name__)
        total["refused_" + type(exc).__name__] += 1
        return "refused", dict(total)
    total.update(result.counts)
    if result.error:
        total["errors"] += 1
        log.warning("fireflies capture error code=%s", result.error)
    return ("partial" if result.error else "succeeded"), dict(total)


def fireflies_tick(settings: Settings, now: datetime | None = None) -> ops.Decision:
    """The rolling Fireflies capture: the last FIREFLIES_CAPTURE_DAYS, at most once per
    FIREFLIES_CAPTURE_INTERVAL_MIN, when the app setting allows it. Deleted meetings
    inside the window are withdrawn afterwards. Backfill windows run from the main
    scheduler tick."""
    now = now or datetime.now(UTC)
    if not settings.fireflies_capture_enabled:
        return ops.Decision(False, "FIREFLIES_CAPTURE_ENABLED app setting is false")
    if settings.fireflies_backfill_since is None:
        return ops.Decision(False, "FIREFLIES_BACKFILL_SINCE is not set (D-M1)")
    with connect(role="ci_ingest") as conn:
        last = conn.execute(
            "SELECT last_fireflies_capture_at AS at FROM ops_settings WHERE id"
        ).fetchone()
        if ops.close_stale_runs(conn, "fireflies_capture", ops.RUNNING_CAPTURE_GRACE, now):
            log.warning("closed stale fireflies capture run(s)")
        running = conn.execute(
            "SELECT 1 FROM ops_runs WHERE kind = 'fireflies_capture' AND status = 'running'"
            " AND started_at > %s LIMIT 1",
            (now - ops.RUNNING_CAPTURE_GRACE,),
        ).fetchone()
        if running:
            return ops.Decision(False, "a fireflies capture is still running")
        if (
            last
            and last["at"]
            and now - last["at"] < timedelta(minutes=settings.fireflies_capture_interval_min)
        ):
            return ops.Decision(False, "fireflies capture ran recently")
        since, until = ff_capture.rolling_window(now, settings.fireflies_capture_days)
        since = max(since, settings.fireflies_backfill_since)
        run_id = ops.start_run(
            conn, "fireflies_capture", "schedule", round((until - since).total_seconds() / 3600, 2)
        )
        conn.execute("UPDATE ops_settings SET last_fireflies_capture_at = %s WHERE id", (now,))
        conn.commit()
    try:
        status, counts = fireflies_capture_now(settings, since=since, until=until)
        error = None
        if status == "succeeded":
            counts["withdrawn"] = ff_capture.withdraw_missing(
                _connect, backends.fireflies_client(settings), since=since, until=until
            )
    except Exception as exc:  # recorded as a code; the next tick tries again
        status, counts, error = "failed", {}, type(exc).__name__[:60]
        log.exception("fireflies capture failed")
    with connect(role="ci_ingest") as conn:
        ops.finish_run(conn, run_id, status, counts, error)
        conn.commit()
    return ops.Decision(True, f"fireflies capture: {status}", "schedule")


def run_capture(settings: Settings, now: datetime | None = None) -> dict[str, int] | None:
    """Capture the ``CAPTURE_HOURS`` before ``now`` (default: the current time),
    ignoring the schedule (CLI and tests)."""
    if not settings.capture_enabled:
        log.info("capture skipped: CAPTURE_ENABLED is false")
        return None
    status, counts = capture_now(settings, settings.capture_hours, now)
    return None if status == "refused" else counts


def scheduler_tick(settings: Settings, now: datetime | None = None) -> ops.Decision:
    """Run a capture if the schedule (or a "run now" request) says one is due."""
    now = now or datetime.now(UTC)
    if not settings.capture_enabled:
        return ops.Decision(False, "CAPTURE_ENABLED app setting is false")
    with connect(role="ci_ingest") as conn:
        decision = ops.capture_decision(ops.read_schedule(conn), now)
        if ops.capture_in_progress(conn, now):
            return ops.Decision(False, "a capture is still running")
        if ops.close_stale_runs(conn, "capture", ops.RUNNING_CAPTURE_GRACE, now):
            log.warning("closed stale capture run(s)")
        if ops.requeue_stale_windows(conn):
            log.warning("put stale backfill window(s) back to pending")
        if not decision.due:
            backfill_pending = ops.next_backfill_window(conn) is not None
            conn.commit()
            if backfill_pending:
                return run_backfill(settings, now)
            return decision
        run_id = ops.begin_capture(conn, decision, now)
        conn.commit()
    try:
        status, counts = capture_now(settings, decision.hours, now)
        error = None
    except Exception as exc:  # recorded as a code; the next tick tries again
        status, counts, error = "failed", {}, type(exc).__name__[:60]
        log.exception("capture failed")
    with connect(role="ci_ingest") as conn:
        ops.finish_run(conn, run_id, status, counts, error)
    return decision


def run_backfill(
    settings: Settings,
    now: datetime | None = None,
    *,
    max_seconds: float = BACKFILL_TICK_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ops.Decision:
    """Work through pending backfill windows, newest first, one capture run each,
    until none is left or ``max_seconds`` have passed since the start. Called from
    the scheduler when no scheduled or requested capture is due; a scheduled
    capture waits until the tick returns."""
    now = now or datetime.now(UTC)
    started = clock()
    windows = 0
    first: dict[str, bool] = {}  # per source: inventory sync before its first window
    refused: tuple[str, ...] = ()
    while clock() - started < max_seconds:
        with connect(role="ci_ingest") as conn:
            window = ops.claim_backfill_window(conn, now, skip_sources=refused)
            conn.commit()
        if window is None:
            break
        windows += 1
        log.info(
            "backfill window %s: %s to %s (%.0f h)",
            window.id,
            window.since,
            window.until,
            window.hours,
        )
        sync = not first.get(window.source, False)
        try:
            if window.source == "fireflies":
                status, counts = fireflies_capture_now(
                    settings, since=window.since, until=window.until, sync=sync
                )
            else:
                status, counts = capture_now(
                    settings, None, now, since=window.since, until=window.until, sync=sync
                )
            error = None
            if status == "refused":  # paused mode or empty scope: the next window would be too
                error = next((k for k in counts if k.startswith("refused_")), "refused")[:60]
        except Exception as exc:  # recorded as a code; the window is marked failed
            status, counts, error = "failed", {}, type(exc).__name__[:60]
            log.exception("backfill window failed")
        first[window.source] = True
        with connect(role="ci_ingest") as conn:
            ops.finish_backfill_window(conn, window, status, counts, error)
            conn.commit()
        if status == "refused":
            log.warning(
                "backfill: %s capture refused (%s); its windows stay pending", window.source, error
            )
            refused = (*refused, window.source)
    reason = f"backfill: {windows} window(s) run" if windows else "backfill: nothing pending"
    return ops.Decision(windows > 0, reason, "backfill")


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
        modelled = backends.with_model(settings, schedule)
        configured = backends.with_switches(
            backends.processing_backends(modelled), settings, schedule
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
        counts.update(
            rate_pending_risks(
                lambda: connect(role="ci_ingest"),
                backends.risk_rater(modelled, configured.meter),
                max_seconds=RISK_RATING_SECONDS,
            )
        )
        counts.update(
            resolve_pending_actions(
                lambda: connect(role="ci_ingest"),
                backends.action_resolver(modelled, configured.meter),
                max_seconds=RESOLUTION_SECONDS,
            )
        )
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
