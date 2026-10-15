"""Mailbox inventory and per-folder delta reconciliation (CLAUDE.md section 6, steps 1-2).

Per-folder delta is the source of truth for capture. Each page's message IDs
are committed as jobs before the next page is read, and the delta link is
persisted only after every page of the round is committed. An expired delta
link (410) drops the checkpoint and resyncs from the backfill boundary;
database uniqueness absorbs the duplicates. Throttling (429) stops that
mailbox for this round without moving its checkpoint.

Capture runs as ``ci_ingest``. Empty scope fails closed: nothing is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from psycopg.types.json import Jsonb

from ci_core import jobs
from ci_core.control import RuntimeState, runtime_state
from ci_core.db import Connection, many, one, one_required, transaction
from ci_core.settings import Settings
from ci_email.graph import (
    SKIPPED_FOLDERS,
    GraphClient,
    GraphDeltaExpired,
    GraphForbidden,
    GraphNotFound,
    GraphThrottled,
)

SCOPE_POLICY_VERSION = "mailbox-scope.v1"
FETCH, WITHDRAW, PROCESS = "email_fetch", "email_withdraw", "process_version"
RECIPIENT_TYPES = {"UserMailbox", "SharedMailbox", "RoomMailbox", "EquipmentMailbox"}


@dataclass
class InventoryResult:
    address: str
    scope_id: str
    state: str
    reason_code: str | None


@dataclass
class MailboxRound:
    address: str
    folders_read: int = 0
    pages: int = 0
    fetch_jobs: int = 0
    withdraw_jobs: int = 0
    duplicates_collapsed: int = 0
    resynced_after_410: int = 0
    stopped: str | None = None  # content-free reason code


@dataclass
class ReconcileReport:
    skipped: str | None = None
    mailboxes: list[MailboxRound] = field(default_factory=list)


def sync_inventory(
    conn: Connection, graph: GraphClient, *, connection_id: str, corpus_id: str
) -> list[InventoryResult]:
    """Record every mailbox the inventory lists, with its observed state.

    Exclusions always win. Shared mailboxes have accountEnabled=false, so that
    flag is never used to filter.
    """
    results: list[InventoryResult] = []
    for mbx in graph.list_mailboxes():
        with transaction(conn, "ci_ingest"):
            excluded = one(
                conn,
                "SELECT 1 FROM exclusions WHERE exclusion_kind = 'scope' AND revoked_at IS NULL"
                " AND scope_kind = 'mailbox' AND native_scope_id = %s",
                (mbx.id,),
            )
        state, reason = "enrolled", None
        if excluded:
            state, reason = "excluded_by_rule", "db_scope_exclusion"
        else:
            try:
                graph.probe_mailbox(mbx.id)
            except GraphForbidden as exc:
                state, reason = "no_exchange_access", exc.code
            except GraphNotFound as exc:
                state, reason = "no_mailbox", exc.code
        recipient_type = mbx.recipient_type if mbx.recipient_type in RECIPIENT_TYPES else "Other"
        with transaction(conn, "ci_ingest"):
            row = one_required(
                conn,
                """
                INSERT INTO source_scopes (connection_id, scope_kind, native_scope_id,
                                           display_label, corpus_id, scope_state,
                                           state_reason_code, state_checked_at, policy_version)
                VALUES (%s, 'mailbox', %s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (connection_id, scope_kind, native_scope_id) DO UPDATE
                SET display_label = EXCLUDED.display_label, scope_state = EXCLUDED.scope_state,
                    state_reason_code = EXCLUDED.state_reason_code, state_checked_at = now()
                RETURNING id
                """,
                (
                    connection_id,
                    mbx.id,
                    mbx.display_name,
                    corpus_id,
                    state,
                    reason,
                    SCOPE_POLICY_VERSION,
                ),
            )
            conn.execute(
                """
                INSERT INTO email_mailboxes (scope_id, primary_smtp_address, recipient_type)
                VALUES (%s, %s, %s)
                ON CONFLICT (scope_id) DO UPDATE
                SET primary_smtp_address = EXCLUDED.primary_smtp_address,
                    recipient_type = EXCLUDED.recipient_type, last_inventory_at = now()
                """,
                (row["id"], mbx.address.lower(), recipient_type),
            )
        results.append(InventoryResult(mbx.address, str(row["id"]), state, reason))
    return results


def capturable_scopes(
    conn: Connection, connection_id: str, dataset_key: str
) -> list[dict[str, object]]:
    """Scopes that are enrolled, approved for the dataset and not excluded."""
    with transaction(conn, "ci_ingest"):
        return many(
            conn,
            """
            SELECT s.id, s.native_scope_id, s.backfill_since, m.primary_smtp_address
            FROM source_scopes s JOIN email_mailboxes m ON m.scope_id = s.id
            WHERE s.connection_id = %(conn)s AND s.scope_kind = 'mailbox'
              AND s.scope_state = 'enrolled'
              AND CASE %(dataset)s WHEN 'test' THEN s.test_approved
                                   WHEN 'production' THEN s.production_approved ELSE false END
              AND NOT EXISTS (SELECT 1 FROM exclusions e
                              WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL
                                AND e.scope_kind = 'mailbox'
                                AND e.native_scope_id = s.native_scope_id)
            ORDER BY m.primary_smtp_address
            """,
            {"conn": connection_id, "dataset": dataset_key},
        )


def _checkpoint_key(connection_id: str, mailbox_id: str, folder_id: str) -> str:
    return f"email:{connection_id}:{mailbox_id}:{folder_id}:delta"


def reconcile(
    conn: Connection, graph: GraphClient, settings: Settings, *, connection_id: str
) -> ReconcileReport:
    report = ReconcileReport()
    runtime: RuntimeState | None = runtime_state(conn)
    if runtime is None or runtime.dataset_key is None:
        report.skipped = "mode_paused"
        return report
    if not runtime.allows("email"):
        report.skipped = "provider_disabled"
        return report
    dataset = runtime.dataset_key
    scopes = capturable_scopes(conn, connection_id, dataset)
    if not scopes:
        report.skipped = "empty_scope"
        return report
    skip = set(SKIPPED_FOLDERS) | (set() if settings.email_include_sent else {"sentitems"})
    for scope in scopes:
        rnd = MailboxRound(str(scope["primary_smtp_address"]))
        report.mailboxes.append(rnd)
        since = scope["backfill_since"]
        if not isinstance(since, datetime):
            rnd.stopped = "no_backfill_boundary"
            continue
        mailbox_id = str(scope["native_scope_id"])
        try:
            folders = [f for f in graph.list_folders(mailbox_id) if f.well_known_name not in skip]
            for folder in folders:
                _reconcile_folder(
                    conn,
                    graph,
                    runtime,
                    rnd,
                    connection_id=connection_id,
                    scope_id=str(scope["id"]),
                    mailbox_id=mailbox_id,
                    folder_id=folder.id,
                    since=since,
                )
                rnd.folders_read += 1
        except GraphThrottled as exc:
            rnd.stopped = f"{exc.code}_retry_after_{exc.retry_after_seconds}s"
        except GraphForbidden as exc:
            rnd.stopped = exc.code
    return report


def _reconcile_folder(
    conn: Connection,
    graph: GraphClient,
    runtime: RuntimeState,
    rnd: MailboxRound,
    *,
    connection_id: str,
    scope_id: str,
    mailbox_id: str,
    folder_id: str,
    since: datetime,
) -> None:
    dataset = runtime.dataset_key
    if dataset is None:
        raise ValueError("reconcile needs an active dataset")
    key = _checkpoint_key(connection_id, mailbox_id, folder_id)
    with transaction(conn, "ci_ingest"):
        cp = one(conn, "SELECT cursor_value FROM checkpoints WHERE checkpoint_key = %s", (key,))
    link: str | None = None if cp is None else cp["cursor_value"].get("delta_link")
    initial = link is None
    resynced = False
    while True:
        try:
            page = graph.message_delta(mailbox_id, folder_id, link, since)
        except GraphDeltaExpired:
            if resynced:
                raise
            resynced, initial, link = True, True, None
            rnd.resynced_after_410 += 1
            with transaction(conn, "ci_ingest"):
                conn.execute("DELETE FROM checkpoints WHERE checkpoint_key = %s", (key,))
            continue
        rnd.pages += 1
        with transaction(conn, "ci_ingest"):
            for change in page.changes:
                job_id = jobs.admit(
                    conn,
                    dataset_key=dataset,
                    config_revision=runtime.revision,
                    job_type=WITHDRAW if change.removed else FETCH,
                    dedupe_key=f"{scope_id}:{change.message_id}",
                    payload={
                        "scope_id": scope_id,
                        "mailbox_id": mailbox_id,
                        "message_id": change.message_id,
                        "folder_id": folder_id,
                    },
                    priority=100 if initial else 50,  # live before backfill
                )
                if job_id is None:
                    rnd.duplicates_collapsed += 1
                elif change.removed:
                    rnd.withdraw_jobs += 1
                else:
                    rnd.fetch_jobs += 1
        if page.next_link:
            link = page.next_link
            continue
        # Every page is committed as jobs; only now persist the delta link.
        with transaction(conn, "ci_ingest"):
            conn.execute(
                """
                INSERT INTO checkpoints (checkpoint_key, dataset_key, cursor_value)
                VALUES (%s, %s, %s)
                ON CONFLICT (checkpoint_key) DO UPDATE
                SET cursor_value = EXCLUDED.cursor_value, updated_at = now()
                """,
                (key, dataset, Jsonb({"delta_link": page.delta_link})),
            )
        return
