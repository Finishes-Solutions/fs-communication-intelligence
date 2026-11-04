"""Fireflies capture: list the meetings of a time window, fetch each transcript, store it.

Per window: page the listing (newest first, 50 per page, splitting on a full
page is unnecessary because ``skip`` continues the same listing), then for each
meeting apply the rules before anything is stored: item exclusions, and which
enrolled, approved team members are on it (organizer, host, attendees,
participants). A meeting none of them is on is stored without a scope, so it
is never published, and counted as ``no_scope``. One communication per
transcript (identity = dataset + connection + transcript id); a new version is
created only when the content changes (title, sentences, summary, attendees),
so re-running a window stores nothing new. A transcript the API no longer
returns is withdrawn by the sweep (``withdraw_missing``).

Counts and ids are logged; content never is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ci_core.db import AppRole, Connection, required
from ci_core.runtime import EligibleScope, active_config, require_fireflies_scope
from ci_fireflies.api import (
    PAGE_SIZE,
    FirefliesClient,
    FirefliesError,
    Throttled,
    TranscriptNotFound,
    TranscriptRef,
)
from ci_fireflies.graphql_http import ref_from_listing

log = logging.getLogger(__name__)

NORMALIZER_VERSION = "fireflies-normalizer-v1"
MAX_WINDOW_DAYS = 93
MAX_PAGES = 200  # 10,000 meetings in one window: split the plan instead
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class CaptureResult:
    counts: Counter[str] = field(default_factory=Counter)
    error: str | None = None


def _email(value: Any) -> str | None:
    if isinstance(value, str) and _EMAIL.match(value.strip()):
        return value.strip().lower()
    return None


def meeting_people(data: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(role, email, display name) for everyone with an address on the meeting."""
    out: list[tuple[str, str, str | None]] = []
    seen: set[tuple[str, str]] = set()

    def add(role: str, email: Any, name: Any = None) -> None:
        e = _email(email)
        if e and (role, e) not in seen:
            seen.add((role, e))
            out.append((role, e, str(name) if name else None))

    add("organizer", data.get("organizer_email"))
    add("host", data.get("host_email"))
    for a in data.get("meeting_attendees") or []:
        if isinstance(a, dict):
            add("attendee", a.get("email"), a.get("displayName") or a.get("name"))
    for p in data.get("participants") or []:
        add("attendee", p)
    return out


def _stamp(seconds: Any) -> str:
    try:
        s = max(0, int(float(seconds)))
    except TypeError, ValueError:
        return "--:--"
    return (
        f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"
        if s >= 3600
        else f"{s // 60:02d}:{s % 60:02d}"
    )


def transcript_text(data: dict[str, Any]) -> str:
    """One line per sentence: ``[mm:ss] Speaker: text``. The processing input."""
    lines = []
    for s in data.get("sentences") or []:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        speaker = str(s.get("speaker_name") or "Speaker").strip()
        lines.append(f"[{_stamp(s.get('start_time'))}] {speaker}: {text}")
    return "\n".join(lines)


def summary_text(data: dict[str, Any]) -> str:
    """The vendor's summary as text, labelled as AI output; empty when there is none."""
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return ""
    parts = []
    for key, label in (
        ("overview", "Overview"),
        ("short_summary", "Short summary"),
        ("action_items", "Action items"),
    ):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{label} (Fireflies AI, unverified):\n{value.strip()}")
    keywords = summary.get("keywords")
    if isinstance(keywords, list) and keywords:
        parts.append("Keywords (Fireflies AI): " + ", ".join(str(k) for k in keywords))
    return "\n\n".join(parts)


def source_revision(data: dict[str, Any]) -> str:
    """sha256 of the content fields: a change in any of them makes a new version."""
    canonical = {
        "title": data.get("title"),
        "date": data.get("date"),
        "duration": data.get("duration"),
        "sentences": [
            [s.get("index"), s.get("speaker_name"), s.get("text"), s.get("start_time")]
            for s in data.get("sentences") or []
        ],
        "summary": data.get("summary"),
        "people": meeting_people(data),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def item_excluded(conn: Connection, connection_id: str, native_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM exclusions WHERE exclusion_kind = 'item' AND revoked_at IS NULL"
            " AND connection_id = %s AND native_id = %s",
            (connection_id, native_id),
        ).fetchone()
        is not None
    )


def store_transcript(
    conn: Connection,
    *,
    dataset: str,
    connection_id: str,
    corpus_id: str,
    scopes_by_email: dict[str, EligibleScope],
    data: dict[str, Any],
    profile: dict[str, Any],
) -> str:
    """Upsert one meeting; returns 'new_version', 'unchanged' or 'no_scope'. Caller commits."""
    ref = ref_from_listing(data)
    people = meeting_people(data)
    scopes = {scopes_by_email[e].scope_id for _, e, _ in people if e in scopes_by_email}
    sentences = data.get("sentences") or []
    comm = required(
        conn.execute(
            """
        INSERT INTO communications (dataset_key, connection_id, native_id, kind, corpus_id, title,
                                    occurred_at, ended_at, duration_seconds, source_url)
        VALUES (%s, %s, %s, 'meeting', %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_key, connection_id, native_id) DO UPDATE
           SET title = EXCLUDED.title, occurred_at = EXCLUDED.occurred_at,
               ended_at = EXCLUDED.ended_at, duration_seconds = EXCLUDED.duration_seconds,
               source_url = EXCLUDED.source_url, last_seen_at = now(),
               lifecycle_state = CASE WHEN communications.lifecycle_state = 'deleted_at_source'
                                      THEN 'active' ELSE communications.lifecycle_state END
        RETURNING id::text
        """,
            (
                dataset,
                connection_id,
                ref.id,
                corpus_id,
                data.get("title"),
                ref.occurred_at,
                ref.occurred_at + timedelta(minutes=ref.duration_minutes),
                int(ref.duration_minutes * 60),
                data.get("transcript_url"),
            ),
        ).fetchone()
    )["id"]
    for scope_id in sorted(scopes):
        conn.execute(
            "INSERT INTO communication_scopes (communication_id, scope_id) VALUES (%s, %s)"
            " ON CONFLICT (communication_id, scope_id) DO UPDATE SET last_seen_at = now()",
            (comm, scope_id),
        )
    info = data.get("meeting_info") if isinstance(data.get("meeting_info"), dict) else {}
    conn.execute(
        """
        INSERT INTO fireflies_meetings (communication_id, organizer_email, host_email, meeting_link,
                                        transcript_url, duration_minutes, sentence_count,
                                        speaker_count, attendee_count, summary_status, has_summary)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (communication_id) DO UPDATE
           SET organizer_email = EXCLUDED.organizer_email, host_email = EXCLUDED.host_email,
               meeting_link = EXCLUDED.meeting_link, transcript_url = EXCLUDED.transcript_url,
               duration_minutes = EXCLUDED.duration_minutes,
               sentence_count = EXCLUDED.sentence_count, speaker_count = EXCLUDED.speaker_count,
               attendee_count = EXCLUDED.attendee_count, summary_status = EXCLUDED.summary_status,
               has_summary = EXCLUDED.has_summary, updated_at = now()
        """,
        (
            comm,
            _email(data.get("organizer_email")),
            _email(data.get("host_email")),
            data.get("meeting_link") or None,
            data.get("transcript_url") or None,
            ref.duration_minutes,
            len(sentences),
            len({s.get("speaker_name") for s in sentences if s.get("speaker_name")}),
            len({e for _, e, _ in people}),
            (info or {}).get("summary_status"),
            bool(summary_text(data)),
        ),
    )
    for role, email, name in sorted(people, key=lambda p: (p[1], p[0])):
        row = conn.execute(
            "INSERT INTO participants (provider, provider_identity, email_normalized, display_name)"
            " VALUES ('fireflies', %s, %s, %s)"
            " ON CONFLICT (provider, provider_identity) DO NOTHING RETURNING id::text",
            (email, email, name),
        ).fetchone()
        if row is None:
            row = required(
                conn.execute(
                    "SELECT id::text, display_name FROM participants"
                    " WHERE provider = 'fireflies' AND provider_identity = %s",
                    (email,),
                ).fetchone()
            )
            if name and row["display_name"] is None:
                conn.execute(
                    "UPDATE participants SET display_name = %s"
                    " WHERE id = %s AND display_name IS NULL",
                    (name, row["id"]),
                )
        conn.execute(
            "INSERT INTO communication_participants (communication_id, participant_id, role)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (comm, row["id"], role),
        )

    revision = source_revision(data)
    profile_json = json.dumps(profile, sort_keys=True)
    if conn.execute(
        "SELECT 1 FROM communication_versions WHERE communication_id = %s"
        " AND source_revision = %s AND profile = %s::jsonb",
        (comm, revision, profile_json),
    ).fetchone():
        return "unchanged" if scopes else "no_scope"
    version = required(
        conn.execute(
            "INSERT INTO communication_versions (communication_id, version_no, source_revision,"
            " profile) VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1"
            " FROM communication_versions WHERE communication_id = %s), %s, %s::jsonb)"
            " RETURNING id::text",
            (comm, comm, revision, profile_json),
        ).fetchone()
    )["id"]
    text = transcript_text(data)
    conn.execute(
        "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256)"
        " VALUES (%s, 'transcript', 'body', %s, %s)",
        (version, text, hashlib.sha256(text.encode()).hexdigest()),
    )
    summary = summary_text(data)
    if summary:
        conn.execute(
            "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256)"
            " VALUES (%s, 'provider_summary', 'summary', %s, %s)",
            (version, summary, hashlib.sha256(summary.encode()).hexdigest()),
        )
    for seq, s in enumerate(sentences):
        start = s.get("start_time")
        end = s.get("end_time")
        conn.execute(
            "INSERT INTO transcript_segments (version_id, seq, speaker_label, start_ms, end_ms,"
            " text) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                version,
                seq,
                str(s.get("speaker_name") or "") or None,
                int(float(start) * 1000) if start is not None else None,
                int(float(end) * 1000) if end is not None else None,
                str(s.get("text") or ""),
            ),
        )
    return "new_version" if scopes else "no_scope"


def list_window(
    client: FirefliesClient, since: datetime, until: datetime
) -> Iterator[TranscriptRef]:
    """Every meeting in [since, until), paging with ``skip`` and deduping by id."""
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        refs = client.list_transcripts(since, until, skip=page * PAGE_SIZE)
        new = [r for r in refs if r.id not in seen]
        for r in new:
            seen.add(r.id)
            yield r
        if len(refs) < PAGE_SIZE or not new:
            return
    raise FirefliesError("window_too_large")


def capture_window(
    connect: Callable[[AppRole | None], Connection],
    client: FirefliesClient,
    *,
    since: datetime,
    until: datetime,
    profile: dict[str, Any],
) -> CaptureResult:
    """Capture every meeting of the window that an eligible team member is on.
    Fails closed on paused mode, a disabled source or an empty scope."""
    span = (until - since).total_seconds() / 86400
    if not 0 < span <= MAX_WINDOW_DAYS:
        raise ValueError(f"a window must be more than 0 and at most {MAX_WINDOW_DAYS} days")
    result = CaptureResult()
    with connect("ci_ingest") as conn:
        config = active_config(conn)
        scopes = require_fireflies_scope(conn)
        connection_id = scopes[0].connection_id
        corpus_id = required(
            conn.execute(
                "SELECT corpus_id::text FROM source_scopes WHERE id = %s", (scopes[0].scope_id,)
            ).fetchone()
        )["corpus_id"]
    dataset = config.dataset or ""
    by_email = {s.address: s for s in scopes}
    try:
        for ref in list_window(client, since, until):
            result.counts["listed"] += 1
            with connect("ci_ingest") as conn:
                if item_excluded(conn, connection_id, ref.id):
                    result.counts["excluded"] += 1
                    continue
            try:
                data = client.get_transcript(ref.id)
            except TranscriptNotFound:
                result.counts["vanished"] += 1
                continue
            with connect("ci_ingest") as conn:
                outcome = store_transcript(
                    conn,
                    dataset=dataset,
                    connection_id=connection_id,
                    corpus_id=corpus_id,
                    scopes_by_email=by_email,
                    data=data,
                    profile=profile,
                )
                conn.commit()
            result.counts[outcome] += 1
    except Throttled as exc:
        result.error = f"throttled_{int(exc.retry_after)}s"
    except FirefliesError as exc:
        result.error = str(exc)[:60]
    log.info("fireflies capture counts=%s error=%s", dict(result.counts), result.error)
    return result


def withdraw_missing(
    connect: Callable[[AppRole | None], Connection],
    client: FirefliesClient,
    *,
    since: datetime,
    until: datetime,
) -> int:
    """Meetings stored for the window that the listing no longer returns are marked
    ``deleted_at_source`` (retrieval stops at once; purge is a later maintenance step).
    Returns how many were withdrawn. Run only after a complete, unthrottled listing."""
    present = {r.id for r in list_window(client, since, until)}
    with connect("ci_ingest") as conn:
        connection_id, _ = _connection(conn)
        rows = conn.execute(
            "SELECT native_id FROM communications WHERE connection_id = %s AND kind = 'meeting'"
            " AND lifecycle_state = 'active' AND occurred_at >= %s AND occurred_at < %s",
            (connection_id, since, until),
        ).fetchall()
        gone = [r["native_id"] for r in rows if r["native_id"] not in present]
        if gone:
            conn.execute(
                "UPDATE communications SET lifecycle_state = 'deleted_at_source'"
                " WHERE connection_id = %s AND native_id = ANY(%s)",
                (connection_id, gone),
            )
        conn.commit()
    return len(gone)


def _connection(conn: Connection) -> tuple[str, str]:
    from ci_fireflies.inventory import connection_and_corpus

    return connection_and_corpus(conn)


def rolling_window(now: datetime | None, days: int) -> tuple[datetime, datetime]:
    until = now or datetime.now(UTC)
    return until - timedelta(days=days), until
