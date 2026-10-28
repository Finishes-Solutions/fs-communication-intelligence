"""Bounded-window capture: store every eligible message received in the last N hours.

Per eligible mailbox: resolve the skipped well-known folders, list messages
received since the window start, and for each one apply the rules in
CLAUDE.md section 6 before anything is stored: skip drafts and skipped folders,
then item and participant exclusions. Each mailbox copy is its own
communication (identity = dataset + connection + immutable id); a new version
is created only when the normalized content changes, so re-running the same
window stores nothing new.

Counts and ids are logged; content never is.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ci_core.db import AppRole, Connection, required
from ci_core.runtime import EligibleScope, active_config, require_capture_scope
from ci_email.graph import SENT_ITEMS, SKIPPED_WELL_KNOWN_FOLDERS, GraphClient, GraphError

log = logging.getLogger(__name__)

NORMALIZER_VERSION = "email-normalizer-v1"
FINGERPRINT_VERSION = "fp-v1-metadata-manifest"
ROLES = (
    ("from", "from"),
    ("sender", "sender"),
    ("toRecipients", "to"),
    ("ccRecipients", "cc"),
    ("bccRecipients", "bcc"),
)


@dataclass
class MailboxResult:
    address: str
    counts: Counter[str] = field(default_factory=Counter)
    error: str | None = None


def _addresses(message: dict[str, Any], key: str) -> list[tuple[str, str | None]]:
    value = message.get(key)
    entries = value if isinstance(value, list) else [value] if value else []
    out = []
    for entry in entries:
        email = (entry or {}).get("emailAddress") or {}
        if email.get("address"):
            out.append((email["address"].strip().lower(), email.get("name")))
    return out


def participants(message: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(role, address, display name) for every addressed participant."""
    return [(role, addr, name) for key, role in ROLES for addr, name in _addresses(message, key)]


def direction(message: dict[str, Any], mailbox_address: str) -> str:
    sender = _addresses(message, "from")
    return "out" if sender and sender[0][0] == mailbox_address else "in"


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _manifest(attachments: list[dict[str, Any]]) -> list[list[Any]]:
    return sorted(
        [a.get("name"), a.get("contentType"), a.get("size"), bool(a.get("isInline"))]
        for a in attachments
    )


def source_revision(message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    """Hash of the normalized content; a change creates a new version."""
    return _sha256(
        {
            "v": NORMALIZER_VERSION,
            "subject": message.get("subject"),
            "body": (message.get("body") or {}).get("content"),
            "participants": participants(message),
            "sent": message.get("sentDateTime"),
            "attachments": _manifest(attachments),
        }
    )


def content_fingerprint(message: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
    """Correlates copies across mailboxes, with the Internet Message-ID. Excludes
    recipients so a BCC copy matches; see docs/EMAIL_DEDUPLICATION.md."""
    return _sha256(
        {
            "v": FINGERPRINT_VERSION,
            "from": _addresses(message, "from")[:1],
            "sent": message.get("sentDateTime"),
            "subject": message.get("subject"),
            "body": (message.get("body") or {}).get("content"),
            "attachments": _manifest(attachments),
        }
    )


def excluded_by_participant(conn: Connection, message: dict[str, Any]) -> bool:
    addresses = sorted({addr for _, addr, _ in participants(message)})
    domains = sorted({addr.split("@", 1)[1] for addr in addresses if "@" in addr})
    row = conn.execute(
        "SELECT 1 FROM exclusions WHERE revoked_at IS NULL AND ("
        " (exclusion_kind = 'participant_address' AND pattern = ANY(%s)) OR"
        " (exclusion_kind = 'participant_domain' AND pattern = ANY(%s))) LIMIT 1",
        (addresses, domains),
    ).fetchone()
    return row is not None


def item_excluded(conn: Connection, connection_id: str, native_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM exclusions WHERE exclusion_kind = 'item' AND revoked_at IS NULL"
            " AND connection_id = %s AND native_id = %s",
            (connection_id, native_id),
        ).fetchone()
        is not None
    )


def store_message(
    conn: Connection,
    *,
    dataset: str,
    scope: EligibleScope,
    corpus_id: str,
    message: dict[str, Any],
    attachments: list[dict[str, Any]],
    profile: dict[str, Any],
) -> str:
    """Upsert one mailbox copy; returns 'new_version' or 'unchanged'. Caller commits."""
    received = message["receivedDateTime"]
    comm = required(
        conn.execute(
            """
        INSERT INTO communications (dataset_key, connection_id, native_id, kind, corpus_id, title,
                                    occurred_at, source_url)
        VALUES (%s, %s, %s, 'email', %s, %s, %s, %s)
        ON CONFLICT (dataset_key, connection_id, native_id) DO UPDATE
           SET title = EXCLUDED.title, source_url = EXCLUDED.source_url, last_seen_at = now()
        RETURNING id::text
        """,
            (
                dataset,
                scope.connection_id,
                message["id"],
                corpus_id,
                message.get("subject"),
                received,
                message.get("webLink"),
            ),
        ).fetchone()
    )["id"]
    conn.execute(
        "INSERT INTO communication_scopes (communication_id, scope_id) VALUES (%s, %s)"
        " ON CONFLICT (communication_id, scope_id) DO UPDATE SET last_seen_at = now()",
        (comm, scope.scope_id),
    )
    conn.execute(
        """
        INSERT INTO email_messages (communication_id, mailbox_scope_id, internet_message_id,
                                    conversation_id, change_key, received_at, sent_at, direction,
                                    parent_folder_id, has_attachments, content_fingerprint)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (communication_id) DO UPDATE
           SET change_key = EXCLUDED.change_key, parent_folder_id = EXCLUDED.parent_folder_id,
               has_attachments = EXCLUDED.has_attachments,
               content_fingerprint = EXCLUDED.content_fingerprint, updated_at = now()
        """,
        (
            comm,
            scope.scope_id,
            message.get("internetMessageId"),
            message.get("conversationId"),
            message.get("changeKey"),
            received,
            message.get("sentDateTime"),
            direction(message, scope.address),
            message.get("parentFolderId"),
            bool(message.get("hasAttachments")),
            content_fingerprint(message, attachments),
        ),
    )
    for role, address, name in participants(message):
        pid = required(
            conn.execute(
                "INSERT INTO participants (provider, provider_identity, email_normalized,"
                " display_name)"
                " VALUES ('email', %s, %s, %s) ON CONFLICT (provider, provider_identity)"
                " DO UPDATE SET display_name = coalesce(EXCLUDED.display_name,"
                " participants.display_name) RETURNING id::text",
                (address, address, name),
            ).fetchone()
        )["id"]
        conn.execute(
            "INSERT INTO communication_participants (communication_id, participant_id, role)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (comm, pid, role),
        )

    revision = source_revision(message, attachments)
    profile_json = json.dumps(profile, sort_keys=True)
    if conn.execute(
        "SELECT 1 FROM communication_versions WHERE communication_id = %s"
        " AND source_revision = %s AND profile = %s::jsonb",
        (comm, revision, profile_json),
    ).fetchone():
        return "unchanged"
    version = required(
        conn.execute(
            "INSERT INTO communication_versions (communication_id, version_no, source_revision,"
            " profile) VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1"
            " FROM communication_versions WHERE communication_id = %s), %s, %s::jsonb)"
            " RETURNING id::text",
            (comm, comm, revision, profile_json),
        ).fetchone()
    )["id"]
    body = (message.get("body") or {}).get("content") or ""
    conn.execute(
        "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256)"
        " VALUES (%s, 'email_body', 'body', %s, %s)",
        (version, body, hashlib.sha256(body.encode()).hexdigest()),
    )
    for a in attachments:
        conn.execute(
            "INSERT INTO email_attachments (version_id, native_attachment_id, name, content_type,"
            " size_bytes, is_inline) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                version,
                a["id"],
                a.get("name"),
                a.get("contentType"),
                int(a.get("size") or 0),
                bool(a.get("isInline")),
            ),
        )
    return "new_version"


def capture_mailbox(
    connect: Callable[[], Connection],
    graph: GraphClient,
    scope: EligibleScope,
    *,
    dataset: str,
    corpus_id: str,
    since: datetime,
    include_sent: bool,
    profile: dict[str, Any],
) -> MailboxResult:
    result = MailboxResult(scope.address)
    try:
        folders = graph.well_known_folder_ids(scope.native_scope_id)
        skip_names = SKIPPED_WELL_KNOWN_FOLDERS | (set() if include_sent else {SENT_ITEMS})
        skip_ids = {fid for name, fid in folders.items() if name in skip_names}
        messages = graph.list_messages(scope.native_scope_id, received_since=since)
    except GraphError as exc:
        result.error = type(exc).__name__
        log.warning("capture failed for scope %s: %s", scope.scope_id, type(exc).__name__)
        return result
    with connect() as conn:
        for message in messages:
            result.counts["listed"] += 1
            if message.get("isDraft"):
                result.counts["skipped_draft"] += 1
                continue
            if message.get("parentFolderId") in skip_ids:
                result.counts["skipped_folder"] += 1
                continue
            if item_excluded(conn, scope.connection_id, message["id"]) or excluded_by_participant(
                conn, message
            ):
                result.counts["excluded_participant_or_item"] += 1
                continue
            try:
                # Graph reports hasAttachments=false for inline-only images, so
                # signature logos are not listed (one call saved per message).
                attachments = (
                    graph.list_attachments(scope.native_scope_id, message["id"])
                    if message.get("hasAttachments")
                    else []
                )
            except GraphError as exc:
                result.counts[f"error_{type(exc).__name__}"] += 1
                continue
            outcome = store_message(
                conn,
                dataset=dataset,
                scope=scope,
                corpus_id=corpus_id,
                message=message,
                attachments=attachments,
                profile=profile,
            )
            conn.commit()
            result.counts[outcome] += 1
    return result


def capture_window(
    connect: Callable[[AppRole | None], Connection],
    graph: GraphClient,
    *,
    hours: float,
    include_sent: bool,
    profile: dict[str, Any],
    workers: int = 6,
    now: datetime | None = None,
) -> list[MailboxResult]:
    """Capture every eligible mailbox. Fails closed on paused mode or empty scope."""
    if not 0 < hours <= 24 * 31:
        raise ValueError("--hours must be greater than 0 and at most 744")
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    with connect("ci_ingest") as conn:
        config = active_config(conn)
        scopes = require_capture_scope(conn)
        corpus = {
            r["id"]: r["corpus_id"]
            for r in conn.execute(
                "SELECT id::text, corpus_id::text FROM source_scopes WHERE id = ANY(%s::uuid[])",
                ([s.scope_id for s in scopes],),
            ).fetchall()
        }
    if config.dataset is None:  # pragma: no cover - require_capture_scope already refused
        raise RuntimeError("paused")

    def one(scope: EligibleScope) -> MailboxResult:
        return capture_mailbox(
            lambda: connect("ci_ingest"),
            graph,
            scope,
            dataset=config.dataset or "",
            corpus_id=corpus[scope.scope_id],
            since=since,
            include_sent=include_sent,
            profile=profile,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, scopes))
