"""Capture Fireflies meetings dated in the last N hours, from the whole workspace.

Listing: the key's default listing plus, unless turned off, each team member
by ``user_id`` (see ci_fireflies.client), de-duplicated by transcript id. For
each meeting, before anything is stored: item exclusions, then the full
transcript is fetched and participant exclusions are checked against the
organizer, host and attendee addresses. A transcript with no sentences yet is
still being processed by Fireflies and is left for a later capture (the
look-back covers that delay).

A meeting is one communication (identity = dataset + connection + transcript
id) linked to the workspace scope. A new version is stored only when the
normalized content changes (an edited transcript, a summary that arrived
later), so re-running a window stores nothing new.

No PII screening or masking runs on meetings (Matthew, 2026-10-28). Counts
and ids are logged; content never is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ci_core.db import AppRole, Connection, required
from ci_core.exclusions import item_excluded, participants_excluded
from ci_core.runtime import active_config
from ci_fireflies.client import (
    FirefliesClient,
    FirefliesError,
    TranscriptNotFound,
    TranscriptStub,
    epoch_ms_to_datetime,
)
from ci_fireflies.setup import Workspace, eligible_workspace

log = logging.getLogger(__name__)

NORMALIZER_VERSION = "fireflies-normalizer-v1"
MAX_CAPTURE_HOURS = 24 * 93
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class CaptureResult:
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def listing_failed(self) -> bool:
        """Nothing could be listed at all (every listing call failed)."""
        return self.counts["list_ok"] == 0 and self.counts["list_errors"] > 0


def _email(value: Any) -> str | None:
    if isinstance(value, str) and _EMAIL.match(value.strip()):
        return value.strip().lower()
    return None


def meeting_participants(t: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(role, address, display name), de-duplicated per role."""
    names = {}
    for a in t.get("meeting_attendees") or []:
        address = _email((a or {}).get("email"))
        if address:
            names[address] = a.get("displayName") or a.get("name") or None
    out: dict[tuple[str, str], str | None] = {}
    for role, value in (("organizer", t.get("organizer_email")), ("host", t.get("host_email"))):
        address = _email(value)
        if address:
            out[(role, address)] = names.get(address)
    for value in [*(t.get("participants") or []), *names]:
        # Fireflies sometimes joins several addresses into one string.
        for part in str(value).split(","):
            address = _email(part)
            if address:
                out[("attendee", address)] = names.get(address)
    return [(role, address, name) for (role, address), name in sorted(out.items())]


def segments(t: dict[str, Any]) -> list[tuple[int, str | None, int | None, int | None, str]]:
    """(seq, speaker, start_ms, end_ms, text) for every non-empty sentence, in order."""
    out = []
    for i, s in enumerate(t.get("sentences") or []):
        text = re.sub(r"\s+", " ", str((s or {}).get("text") or "")).strip()
        if not text:
            continue
        seq = s.get("index") if isinstance(s.get("index"), int) else i
        start, end = s.get("start_time"), s.get("end_time")
        start_ms = round(float(start) * 1000) if isinstance(start, int | float) else None
        end_ms = round(float(end) * 1000) if isinstance(end, int | float) else None
        if start_ms is not None and end_ms is not None and end_ms < start_ms:
            end_ms = start_ms
        speaker = (s.get("speaker_name") or "").strip() or None
        out.append((seq, speaker, start_ms, end_ms, text))
    # Unique, ordered sequence numbers even if the provider repeats an index.
    out.sort(key=lambda x: x[0])
    seen: set[int] = set()
    unique = []
    for row in out:
        if row[0] not in seen:
            seen.add(row[0])
            unique.append(row)
    return unique


def provider_summary(t: dict[str, Any]) -> str:
    """Fireflies' own AI summary as plain text ('' when there is none)."""
    summary = t.get("summary") or {}
    parts: list[str] = []
    for label, key in (("Overview", "overview"), ("Action items", "action_items")):
        value = summary.get(key)
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value if v)
        if isinstance(value, str) and value.strip():
            parts.append(f"{label}:\n{value.strip()}")
    keywords = summary.get("keywords")
    if isinstance(keywords, list) and keywords:
        parts.append("Keywords: " + ", ".join(str(k) for k in keywords if k))
    return "\n\n".join(parts)


def source_revision(t: dict[str, Any]) -> str:
    """Hash of the normalized content; a change creates a new version."""
    canonical = {
        "v": NORMALIZER_VERSION,
        "title": t.get("title"),
        "date": t.get("date"),
        "duration": t.get("duration"),
        "participants": meeting_participants(t),
        "segments": segments(t),
        "summary": provider_summary(t),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def store_meeting(
    conn: Connection,
    *,
    dataset: str,
    workspace: Workspace,
    transcript: dict[str, Any],
    profile: dict[str, Any],
) -> str:
    """Upsert one meeting; returns 'new_version' or 'unchanged'. Caller commits."""
    occurred = epoch_ms_to_datetime(transcript.get("date"))
    if occurred is None:
        raise ValueError("transcript has no date")
    duration = transcript.get("duration")
    seconds = round(float(duration) * 60) if isinstance(duration, int | float) else None
    ended = occurred + timedelta(seconds=seconds) if seconds else None
    comm = required(
        conn.execute(
            """
            INSERT INTO communications (dataset_key, connection_id, native_id, kind, corpus_id,
                                        title, occurred_at, ended_at, duration_seconds, source_url)
            VALUES (%s, %s, %s, 'meeting', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (dataset_key, connection_id, native_id) DO UPDATE
               SET title = EXCLUDED.title, occurred_at = EXCLUDED.occurred_at,
                   ended_at = EXCLUDED.ended_at, duration_seconds = EXCLUDED.duration_seconds,
                   source_url = EXCLUDED.source_url, last_seen_at = now()
            RETURNING id::text
            """,
            (
                dataset,
                workspace.connection_id,
                transcript["id"],
                workspace.corpus_id,
                transcript.get("title"),
                occurred,
                ended,
                seconds,
                transcript.get("transcript_url"),
            ),
        ).fetchone()
    )["id"]
    conn.execute(
        "INSERT INTO communication_scopes (communication_id, scope_id) VALUES (%s, %s)"
        " ON CONFLICT (communication_id, scope_id) DO UPDATE SET last_seen_at = now()",
        (comm, workspace.scope_id),
    )
    rows = segments(transcript)
    summary = provider_summary(transcript)
    conn.execute(
        """
        INSERT INTO fireflies_meetings (communication_id, organizer_email, host_email,
                                        meeting_link, sentence_count, has_provider_summary)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (communication_id) DO UPDATE
           SET organizer_email = EXCLUDED.organizer_email, host_email = EXCLUDED.host_email,
               meeting_link = EXCLUDED.meeting_link, sentence_count = EXCLUDED.sentence_count,
               has_provider_summary = EXCLUDED.has_provider_summary, updated_at = now()
        """,
        (
            comm,
            _email(transcript.get("organizer_email")),
            _email(transcript.get("host_email")),
            transcript.get("meeting_link"),
            len(rows),
            bool(summary),
        ),
    )
    for role, address, name in meeting_participants(transcript):
        pid = required(
            conn.execute(
                "INSERT INTO participants (provider, provider_identity, email_normalized,"
                " display_name) VALUES ('fireflies', %s, %s, %s)"
                " ON CONFLICT (provider, provider_identity) DO UPDATE"
                " SET display_name = coalesce(participants.display_name, EXCLUDED.display_name)"
                " RETURNING id::text",
                (address, address, name),
            ).fetchone()
        )["id"]
        conn.execute(
            "INSERT INTO communication_participants (communication_id, participant_id, role)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (comm, pid, role),
        )

    revision = source_revision(transcript)
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
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO transcript_segments (version_id, seq, speaker_label, start_ms, end_ms,"
            " text) VALUES (%s, %s, %s, %s, %s, %s)",
            [(version, *row) for row in rows],
        )
    if summary:
        conn.execute(
            "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256)"
            " VALUES (%s, 'provider_summary', 'fireflies', %s, %s)",
            (version, summary, hashlib.sha256(summary.encode()).hexdigest()),
        )
    return "new_version"


def _list_all(
    client: FirefliesClient,
    since: datetime,
    until: datetime,
    *,
    per_user: bool,
    counts: Counter[str],
) -> list[TranscriptStub]:
    found: dict[str, TranscriptStub] = {}

    def add(stubs: Iterable[TranscriptStub], source: str) -> None:
        counts["list_ok"] += 1
        for s in stubs:
            if s.id not in found:
                found[s.id] = s
                counts[f"found_via_{source}"] += 1

    try:
        add(client.list_transcripts(since, until), "default")
    except FirefliesError as exc:
        counts["list_errors"] += 1
        log.warning("fireflies default listing failed: %s", exc)
    if per_user:
        try:
            users = client.list_users()
        except FirefliesError as exc:
            counts["list_errors"] += 1
            log.warning("fireflies user listing failed: %s", exc)
            users = []
        counts["workspace_users"] = len(users)
        for user in users:
            try:
                add(client.list_transcripts(since, until, user.user_id), "user")
            except FirefliesError as exc:
                counts["list_errors"] += 1
                log.warning("fireflies listing failed for user %s: %s", user.user_id, exc)
    return list(found.values())


def capture_meetings(
    connect: Callable[[AppRole | None], Connection],
    client: FirefliesClient,
    *,
    hours: float,
    profile: dict[str, Any],
    per_user: bool = True,
    now: datetime | None = None,
) -> CaptureResult:
    """Capture every meeting dated in the last ``hours``. Fails closed on paused mode,
    a disabled source or no approved workspace (EmptyScopeError)."""
    if not 0 < hours <= MAX_CAPTURE_HOURS:
        raise ValueError(f"hours must be more than 0 and at most {MAX_CAPTURE_HOURS} (93 days)")
    until = now or datetime.now(UTC)
    since = until - timedelta(hours=hours)
    with connect("ci_ingest") as conn:
        config = active_config(conn)
        workspace = eligible_workspace(conn)
    dataset = config.dataset or ""
    result = CaptureResult()
    counts = result.counts
    stubs = _list_all(client, since, until, per_user=per_user, counts=counts)
    counts["listed"] = len(stubs)
    with connect("ci_ingest") as conn:
        for stub in sorted(stubs, key=lambda s: s.id):
            if item_excluded(conn, workspace.connection_id, stub.id):
                counts["excluded_item"] += 1
                continue
            try:
                transcript = client.get_transcript(stub.id)
            except TranscriptNotFound:
                counts["not_found"] += 1
                continue
            except FirefliesError as exc:
                counts["error_fetch"] += 1
                log.warning("fireflies fetch failed for %s: %s", stub.id, exc)
                continue
            if participants_excluded(conn, (a for _, a, _ in meeting_participants(transcript))):
                counts["excluded_participant"] += 1
                continue
            if not segments(transcript):
                counts["not_ready"] += 1
                continue
            try:
                outcome = store_meeting(
                    conn,
                    dataset=dataset,
                    workspace=workspace,
                    transcript=transcript,
                    profile=profile,
                )
                conn.commit()
            except (psycopg.Error, ValueError) as exc:  # one meeting must not stop the run
                conn.rollback()
                counts[f"error_store_{type(exc).__name__}"] += 1
                log.warning("meeting %s not stored: %s", stub.id, type(exc).__name__)
                continue
            counts[outcome] += 1
    log.info("fireflies capture counts=%s", dict(counts))
    return result
