"""Scheduled jobs run by the ingestion Function App (apps/ingestion).

Both are off until switched on (``CAPTURE_ENABLED``, ``PROCESSING_ENABLED``),
and both obey the database mode: paused or empty scope means nothing runs.
They log ids, counts and codes only.
"""

from __future__ import annotations

import logging
from collections import Counter

from ci_cli import backends
from ci_cli.pipeline import capture_profile, connection_and_corpus
from ci_core.db import AppRole, Connection, connect
from ci_core.runtime import EmptyScopeError, RuntimeConfigError
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.inventory import sync_inventory
from ci_email.process import process_all

log = logging.getLogger("ci.scheduled")


def _connect(role: AppRole | None) -> Connection:
    return connect(role=role or "ci_ingest")


def run_capture(settings: Settings) -> dict[str, int] | None:
    """Inventory sync, then capture the last ``CAPTURE_HOURS`` from every eligible mailbox."""
    if not settings.capture_enabled:
        log.info("capture skipped: CAPTURE_ENABLED is false")
        return None
    graph = backends.graph_client(settings)
    with connect(role="ci_ingest") as conn:
        connection, corpus = connection_and_corpus(conn, settings)
        states = sync_inventory(conn, graph, connection, corpus, "ingestion-function").states
        conn.commit()
    log.info("inventory states=%s", states)
    try:
        results = capture_window(
            _connect,
            graph,
            hours=settings.capture_hours,
            include_sent=settings.email_include_sent,
            profile=capture_profile(settings),
            workers=settings.capture_workers,
        )
    except (EmptyScopeError, RuntimeConfigError) as exc:
        log.warning("capture refused: %s", type(exc).__name__)
        return None
    total: Counter[str] = Counter()
    for r in results:
        total.update(r.counts)
        if r.error:
            total["mailbox_errors"] += 1
            log.warning("capture error scope=%s code=%s", r.address.split("@")[-1], r.error)
    log.info("capture mailboxes=%d counts=%s", len(results), dict(total))
    return dict(total)


def run_processing(settings: Settings) -> dict[str, int] | None:
    if not settings.processing_enabled:
        log.info("processing skipped: PROCESSING_ENABLED is false")
        return None
    counts = process_all(
        lambda: connect(role="ci_ingest"),
        backends.processing_backends(settings),
        workers=settings.processing_workers,
        max_seconds=settings.processing_minutes * 60,
    )
    log.info("processing counts=%s", dict(counts))
    return dict(counts)
