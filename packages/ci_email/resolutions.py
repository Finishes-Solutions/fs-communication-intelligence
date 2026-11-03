"""Check whether action items have been dealt with over email (migration 0026).

Runs after processing in each scheduled run (and from ``ci resolve``), for
published action items (latest classification says ``action_required`` or
``approval_required`` = yes) that have at least one later published email in
the same conversation and either no check yet or a reply newer than the last
check, newest activity first, a bounded batch at a time. Mailbox copies of the
same reply are read once (by Internet Message-ID). Identical threads in several
mailboxes reuse the first answer through the input hash. A failed call leaves
the item as it was; the next run tries again.

Conversation membership comes from Graph's ``conversationId`` as recorded per
mailbox copy. Whether every mailbox records the same id for one thread is not
verified; a reply recorded under another id is simply not seen.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg

from ci_core.db import Connection
from ci_processing.resolution import (
    MAX_REPLIES,
    RESOLUTION_VERSION,
    ActionResolver,
    Reply,
    Resolution,
    ResolutionInput,
)

log = logging.getLogger(__name__)

_PENDING = """
WITH actions AS (
    SELECT v.id AS version_id, c.id AS communication_id, c.title, em.conversation_id,
           em.received_at, em.internet_message_id, r.action_text,
           ar.status AS last_status, ar.checked_through
    FROM communication_versions v
    JOIN communications c ON c.active_version_id = v.id
    JOIN email_messages em ON em.communication_id = c.id
    JOIN LATERAL (SELECT r.id, r.action_text FROM classification_runs r
                  WHERE r.version_id = v.id ORDER BY r.id DESC LIMIT 1) r ON true
    LEFT JOIN action_resolutions ar ON ar.version_id = v.id
    WHERE v.processing_state = 'published'
      AND em.conversation_id IS NOT NULL
      AND (ar.version_id IS NULL OR ar.status <> 'resolved')
      AND EXISTS (SELECT 1 FROM classification_labels l
                  WHERE l.run_id = r.id AND l.decision = 'yes'
                    AND l.label IN ('action_required', 'approval_required'))
)
SELECT a.version_id::text AS version_id, a.communication_id::text AS communication_id,
       a.title, a.conversation_id, a.received_at, a.internet_message_id, a.action_text,
       latest.received_at AS latest_reply_at,
       (SELECT t.content FROM version_texts t
        WHERE t.version_id = a.version_id AND t.text_kind = 'email_body' LIMIT 1) AS body
FROM actions a
JOIN LATERAL (
    SELECT max(em2.received_at) AS received_at
    FROM email_messages em2
    JOIN communications c2 ON c2.id = em2.communication_id
    JOIN communication_versions v2 ON v2.id = c2.active_version_id
    WHERE em2.conversation_id = a.conversation_id
      AND em2.received_at > a.received_at
      AND v2.processing_state = 'published'
      AND em2.internet_message_id IS DISTINCT FROM a.internet_message_id
) latest ON latest.received_at IS NOT NULL
WHERE a.checked_through IS NULL OR latest.received_at > a.checked_through
ORDER BY latest.received_at DESC
LIMIT %s
"""

_REPLIES = """
SELECT DISTINCT ON (coalesce(em.internet_message_id, c.id::text))
       c.id::text AS communication_id, em.received_at, c.title,
       (SELECT p.email_normalized FROM communication_participants cp
          JOIN participants p ON p.id = cp.participant_id
         WHERE cp.communication_id = c.id AND cp.role = 'from' LIMIT 1) AS sender,
       (SELECT t.content FROM version_texts t
         WHERE t.version_id = v.id AND t.text_kind = 'email_body' LIMIT 1) AS body
FROM email_messages em
JOIN communications c ON c.id = em.communication_id
JOIN communication_versions v ON v.id = c.active_version_id
WHERE em.conversation_id = %s AND em.received_at > %s
  AND v.processing_state = 'published'
  AND em.internet_message_id IS DISTINCT FROM %s
ORDER BY coalesce(em.internet_message_id, c.id::text), em.received_at
"""


def _replies(conn: Connection, row: dict[str, Any]) -> list[Reply]:
    rows = conn.execute(
        _REPLIES, (row["conversation_id"], row["received_at"], row["internet_message_id"])
    ).fetchall()
    rows.sort(key=lambda r: r["received_at"])
    return [
        Reply(
            r["communication_id"],
            r["sender"],
            r["received_at"].isoformat(),
            r["title"],
            r["body"] or "",
        )
        for r in rows[-MAX_REPLIES:]
    ]


def _reuse(conn: Connection, sha: str, model: str) -> Resolution | None:
    row = conn.execute(
        "SELECT status, confidence, reason FROM action_resolutions"
        " WHERE input_sha256 = %s AND model = %s AND resolution_version = %s LIMIT 1",
        (sha, model, RESOLUTION_VERSION),
    ).fetchone()
    if row is None:
        return None
    # The resolving email differs per mailbox copy; it is re-derived below from this
    # copy's replies when the answer names one (resolver_index None keeps it unset).
    return Resolution(row["status"], row["confidence"], row["reason"], None, model)


def _save(
    conn: Connection,
    row: dict[str, Any],
    replies: list[Reply],
    sha: str,
    answer: Resolution,
) -> None:
    resolver = (
        replies[answer.resolver_index]
        if answer.resolver_index is not None and answer.resolver_index < len(replies)
        else (replies[-1] if answer.status == "resolved" and replies else None)
    )
    conn.execute(
        """
        INSERT INTO action_resolutions (version_id, status, confidence, reason,
            resolver_communication_id, resolver_at, checked_through, replies_seen,
            input_sha256, model, resolution_version)
        VALUES (%s, %s, %s, %s, %s::uuid, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (version_id) DO UPDATE SET
            status = EXCLUDED.status, confidence = EXCLUDED.confidence,
            reason = EXCLUDED.reason,
            resolver_communication_id = EXCLUDED.resolver_communication_id,
            resolver_at = EXCLUDED.resolver_at, checked_through = EXCLUDED.checked_through,
            replies_seen = EXCLUDED.replies_seen, input_sha256 = EXCLUDED.input_sha256,
            model = EXCLUDED.model, resolution_version = EXCLUDED.resolution_version,
            checked_at = now()
        """,
        (
            row["version_id"],
            answer.status,
            answer.confidence,
            answer.reason,
            resolver.communication_id if resolver else None,
            resolver.received_at if resolver else None,
            row["latest_reply_at"],
            len(replies),
            sha,
            answer.model,
            RESOLUTION_VERSION,
        ),
    )


def resolve_one(conn: Connection, resolver: ActionResolver, row: dict[str, Any]) -> str:
    """Check one pending action item; returns the outcome counted by the run."""
    replies = _replies(conn, row)
    if not replies:  # the reply was withdrawn between the listing and now
        return "resolution_no_replies"
    item = ResolutionInput(
        row["title"] or "", row["body"] or "", row["action_text"], tuple(replies)
    )
    sha = item.sha256()
    answer = _reuse(conn, sha, resolver.model)
    outcome = "resolution_reused"
    if answer is None:
        try:
            answer = resolver.resolve(item)
        except Exception as exc:  # content-free code; the next run tries again
            log.warning("action resolution failed: %s", type(exc).__name__)
            return "resolution_failed"
        outcome = f"actions_{answer.status}"
    _save(conn, row, replies, sha, answer)
    conn.commit()
    return outcome


def resolve_pending_actions(
    connect: Callable[[], Connection],
    resolver: ActionResolver,
    *,
    limit: int = 400,
    workers: int = 8,
    max_seconds: float | None = 180.0,
) -> Counter[str]:
    """Check up to ``limit`` action items with new replies, newest activity first,
    within ``max_seconds``. Returns nothing on a database without migration 0026."""
    counts: Counter[str] = Counter()
    try:
        with connect() as conn:
            rows = conn.execute(_PENDING, (limit,)).fetchall()
            conn.commit()
    except psycopg.Error as exc:
        log.warning("action resolutions skipped: %s", type(exc).__name__)
        return counts
    if not rows:
        return counts
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    queue = list(rows)
    lock = threading.Lock()

    def take() -> dict[str, Any] | None:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        with lock:
            return queue.pop(0) if queue else None

    def worker() -> Counter[str]:
        local: Counter[str] = Counter()
        with connect() as conn:
            while (row := take()) is not None:
                local[resolve_one(conn, resolver, row)] += 1
        return local

    n = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=n) as pool:
        for result in pool.map(lambda _: worker(), range(n)):
            counts.update(result)
    return counts
