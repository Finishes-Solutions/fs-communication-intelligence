"""Examples from Label Review for the classifier (migration 0023).

A reviewer's answers in the admin console's Label Review are the ground truth for
measuring the AI (Results tab). They also go back to the model: each
classification call shows up to ``ops_settings.review_examples`` reviewed emails
with the reviewer's answers, so it judges similar mail the way the reviewer did.

Which examples: corrections first (emails where the reviewer disagreed with the
model's decision on some label), from the email's own sender domain when any
exist, then the newest. Only published, active emails are used (an email that
was since withdrawn, excluded or restricted is never shown), each as its
subject and the first few hundred characters of its stored text, which is
already masked. The email being classified is never its own example.

This changes what the model sees, not the thresholds: those still change only
by a person (CLAUDE.md section 3a).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg

from ci_core.db import Connection
from ci_processing.classification import (
    MAX_EXAMPLE_EXCERPT,
    MAX_EXAMPLE_SUBJECT,
    ReviewedExample,
)

log = logging.getLogger(__name__)

POOL_SIZE = 200  # most recently reviewed emails considered
POOL_SECONDS = 300.0  # re-read at most this often (about once per processing run)
DEFAULT_COUNT = 6

# The most recently reviewed emails and, per label, the latest answer (several
# reviewers: the latest one wins).
_REVIEWED = """
    SELECT communication_id::text AS id
    FROM label_feedback WHERE subject_kind = 'label'
    GROUP BY communication_id ORDER BY max(updated_at) DESC LIMIT %s"""

_EXAMPLES = """
    WITH answers AS (
        SELECT DISTINCT ON (f.communication_id, f.subject)
               f.communication_id, f.subject AS label, f.applies
        FROM label_feedback f
        WHERE f.subject_kind = 'label' AND f.communication_id = ANY(%(ids)s::uuid[])
        ORDER BY f.communication_id, f.subject, f.updated_at DESC)
    SELECT pc.id::text AS id, pc.title,
           (SELECT left(t.content, %(chars)s) FROM version_texts t
            WHERE t.version_id = pc.active_version_id AND t.text_kind = 'email_body'
            LIMIT 1) AS body,
           (SELECT p.email_normalized FROM communication_participants cp
            JOIN participants p ON p.id = cp.participant_id
            WHERE cp.communication_id = pc.id AND cp.role = 'from' LIMIT 1) AS sender,
           a.label, a.applies, l.decision AS ai
    FROM published_communications pc
    JOIN answers a ON a.communication_id = pc.id
    LEFT JOIN LATERAL (SELECT r.id FROM classification_runs r
                       WHERE r.version_id = pc.active_version_id
                       ORDER BY r.id DESC LIMIT 1) run ON true
    LEFT JOIN classification_labels l ON l.run_id = run.id AND l.label = a.label
    WHERE pc.id = ANY(%(ids)s::uuid[])"""


@dataclass(frozen=True)
class Candidate:
    communication_id: str
    example: ReviewedExample
    rank: int  # 0 = most recently reviewed

    @property
    def corrects(self) -> bool:
        return bool(self.example.ai_was_wrong_on)


def _domain(address: str | None) -> str:
    return (address or "").rsplit("@", 1)[-1].lower() if address and "@" in address else ""


def load_pool(conn: Connection, size: int = POOL_SIZE) -> list[Candidate]:
    """The most recently reviewed published emails as examples, newest first."""
    ids = [r["id"] for r in conn.execute(_REVIEWED, (size,)).fetchall()]
    if not ids:
        return []
    rows = conn.execute(_EXAMPLES, {"ids": ids, "chars": MAX_EXAMPLE_EXCERPT}).fetchall()
    by_id: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        by_id.setdefault(r["id"], []).append(r)
    pool = []
    for rank, cid in enumerate(ids):
        answers = by_id.get(cid)
        if not answers:  # no longer published
            continue
        first = answers[0]
        yes = sorted(str(a["label"]) for a in answers if a["applies"])
        no = sorted(str(a["label"]) for a in answers if not a["applies"])
        # The AI was wrong where its decision differs from the answer (an
        # uncertain or missing decision counts as wrong when the reviewer answered).
        wrong = sorted(
            str(a["label"])
            for a in answers
            if (a["ai"] == "yes") != bool(a["applies"]) or a["ai"] not in ("yes", "no")
        )
        text = " ".join(str(first["body"] or "").split())
        pool.append(
            Candidate(
                cid,
                ReviewedExample(
                    subject=str(first["title"] or "")[:MAX_EXAMPLE_SUBJECT],
                    excerpt=text[:MAX_EXAMPLE_EXCERPT],
                    sender_domain=_domain(str(first["sender"] or "")),
                    yes=tuple(yes),
                    no=tuple(no),
                    ai_was_wrong_on=tuple(wrong),
                ),
                rank,
            )
        )
    return pool


def pick_examples(
    pool: Sequence[Candidate], count: int, *, sender: str = "", exclude: str | None = None
) -> tuple[ReviewedExample, ...]:
    """Up to ``count`` examples: corrections before confirmations, the sender's own
    domain first, then the most recently reviewed."""
    if count <= 0:
        return ()
    domain = _domain(sender)
    usable = [c for c in pool if c.communication_id != exclude]
    usable.sort(
        key=lambda c: (
            not c.corrects,
            not (domain and c.example.sender_domain == domain),
            c.rank,
        )
    )
    return tuple(c.example for c in usable[:count])


@dataclass
class ReviewCache:
    """The example pool, re-read at most every ``seconds`` (one per processing run)."""

    seconds: float = POOL_SECONDS
    _at: float = 0.0
    _pool: list[Candidate] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, conn: Connection) -> list[Candidate]:
        with self._lock:
            if self._at and time.monotonic() - self._at < self.seconds:
                return self._pool
        try:
            with conn.transaction():
                pool = load_pool(conn)
        except psycopg.Error as exc:  # e.g. before migration 0023
            log.warning("review examples not read: %s", type(exc).__name__)
            pool = []
        with self._lock:
            self._at, self._pool = time.monotonic(), pool
        return pool
