"""Process captured meeting versions: AI analysis, chunk, embed, publish.

Workers claim ``fetched`` meeting versions with ``FOR UPDATE SKIP LOCKED`` and
finish each in one transaction (the same publication rules as email: the new
version becomes ``published``, the prior one ``superseded`` and
``communications.active_version_id`` moves, all at once). A failure marks the
version ``failed`` with a content-free code; ``ci process --retry-failed``
or ``ci fireflies process --retry-failed`` puts it back.

No PII screening, prompt-attack screening or masking runs on meetings
(Matthew, 2026-10-28). Each version gets a ``policy_decisions`` row saying so,
so the gap is visible in the data. The transcript is still treated as untrusted
text by the analyzer prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ci_core.db import Connection
from ci_core.drain import drain
from ci_email.process import known_names
from ci_processing.azure import AzureServiceError
from ci_processing.chunking import Segment, chunk_segments, chunk_text
from ci_processing.embedding import Embedder
from ci_processing.meetings import (
    MEETING_POLICY_VERSION,
    ActionItem,
    MeetingAnalysis,
    MeetingAnalyzer,
    MeetingInput,
    with_known_names,
)

log = logging.getLogger(__name__)

NOT_SCREENED_POLICY = "fireflies-no-screening-2026-10-28"


@dataclass
class MeetingBackends:
    analyzer: MeetingAnalyzer
    embedder: Embedder


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _claim(conn: Connection) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT v.id::text AS version_id, c.id::text AS communication_id, c.native_id,
               c.title, c.occurred_at, m.organizer_email
        FROM communication_versions v
        JOIN communications c ON c.id = v.communication_id
        JOIN fireflies_meetings m ON m.communication_id = c.id
        WHERE v.processing_state = 'fetched' AND c.dataset_key = ci.active_dataset()
        ORDER BY v.created_at
        FOR UPDATE OF v SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()


def _segments(conn: Connection, version_id: str) -> list[Segment]:
    return [
        Segment(r["seq"], r["speaker_label"], r["text"], r["start_ms"], r["end_ms"])
        for r in conn.execute(
            "SELECT seq, speaker_label, start_ms, end_ms, text FROM transcript_segments"
            " WHERE version_id = %s ORDER BY seq",
            (version_id,),
        ).fetchall()
    ]


def _summary(conn: Connection, version_id: str) -> str:
    row = conn.execute(
        "SELECT content FROM version_texts WHERE version_id = %s AND text_kind = 'provider_summary'"
        " ORDER BY ref LIMIT 1",
        (version_id,),
    ).fetchone()
    return row["content"] if row else ""


def _attendees(conn: Connection, communication_id: str) -> list[str]:
    return [
        r["email_normalized"]
        for r in conn.execute(
            "SELECT DISTINCT p.email_normalized FROM communication_participants cp"
            " JOIN participants p ON p.id = cp.participant_id"
            " WHERE cp.communication_id = %s AND p.email_normalized IS NOT NULL ORDER BY 1",
            (communication_id,),
        ).fetchall()
    ]


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"{s.speaker or 'Unknown speaker'}: {s.text}" for s in segments)


def _analyze(conn: Connection, analyzer: MeetingAnalyzer, item: MeetingInput) -> MeetingAnalysis:
    """Analyze, or reuse the stored result for identical input, model and policy."""
    known = known_names(conn)
    prior = conn.execute(
        "SELECT * FROM meeting_analyses WHERE input_sha256 = %s AND model = %s"
        " AND policy_version = %s ORDER BY id LIMIT 1",
        (item.sha256(), analyzer.model, MEETING_POLICY_VERSION),
    ).fetchone()
    if prior:
        result = MeetingAnalysis(
            prior["model"],
            prior["policy_version"],
            prior["input_sha256"],
            prior["probabilities"],
            prior["summary"],
            tuple(prior["decisions"]),
            tuple(
                ActionItem(a["task"], a.get("owner"), a.get("owner_email"), a.get("due"))
                for a in prior["action_items"]
            ),
            tuple(prior["risks"]),
            tuple(prior["properties"]),
            tuple(prior["entities"]),
            prior["input_truncated"],
        )
    else:
        result = analyzer.analyze(item, known)
    return with_known_names(result, known)


def _store_analysis(conn: Connection, version_id: str, a: MeetingAnalysis) -> None:
    conn.execute(
        """
        INSERT INTO meeting_analyses (version_id, model, policy_version, input_sha256, summary,
            decisions, action_items, risks, properties, entities, probabilities, yes_labels,
            uncertain_labels, input_truncated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (version_id, model, policy_version) DO UPDATE SET
            input_sha256 = EXCLUDED.input_sha256, summary = EXCLUDED.summary,
            decisions = EXCLUDED.decisions, action_items = EXCLUDED.action_items,
            risks = EXCLUDED.risks, properties = EXCLUDED.properties,
            entities = EXCLUDED.entities, probabilities = EXCLUDED.probabilities,
            yes_labels = EXCLUDED.yes_labels, uncertain_labels = EXCLUDED.uncertain_labels,
            input_truncated = EXCLUDED.input_truncated, created_at = now()
        """,
        (
            version_id,
            a.model,
            a.policy_version,
            a.input_sha256,
            a.summary,
            list(a.decisions),
            json.dumps([i.public() for i in a.action_items]),
            list(a.risks),
            list(a.properties),
            list(a.entities),
            json.dumps(dict(a.probabilities)),
            a.yes_labels(),
            a.uncertain_labels(),
            a.input_truncated,
        ),
    )


def _chunks(job: dict[str, Any], segments: list[Segment], summary: str) -> list[dict[str, Any]]:
    header = (
        f"Meeting: {job['title'] or ''}\nDate: {job['occurred_at']:%Y-%m-%d}\n"
        f"Organizer: {job['organizer_email'] or ''}\n"
    )
    out: list[dict[str, Any]] = []
    for seq, c in enumerate(chunk_segments(segments)):
        out.append(
            {
                "kind": "transcript_window",
                "seq": seq,
                "segment_from": c.segment_from,
                "segment_to": c.segment_to,
                "start_ms": c.start_ms,
                "end_ms": c.end_ms,
                "speakers": list(c.speakers),
                "text": c.text,
                "embed": f"{header}Speakers: {', '.join(c.speakers)}\n\n{c.text}",
            }
        )
    for seq, text in enumerate(chunk_text(summary)):
        out.append(
            {
                "kind": "provider_summary",
                "seq": seq,
                "segment_from": None,
                "segment_to": None,
                "start_ms": None,
                "end_ms": None,
                "speakers": [],
                "text": text,
                "embed": f"{header}Fireflies summary (AI-generated)\n\n{text}",
            }
        )
    return out


def _unpublish(conn: Connection, job: dict[str, Any], state: str, reason: str) -> None:
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'superseded'"
        " WHERE communication_id = %s AND processing_state = 'published' AND id <> %s",
        (job["communication_id"], job["version_id"]),
    )
    conn.execute(
        "UPDATE communication_versions SET processing_state = %s, hold_reason_code = %s"
        " WHERE id = %s",
        (state, reason, job["version_id"]),
    )
    conn.execute(
        "UPDATE communications SET active_version_id = NULL WHERE id = %s",
        (job["communication_id"],),
    )


def process_meeting(conn: Connection, job: dict[str, Any], backends: MeetingBackends) -> str:
    conn.execute(
        "INSERT INTO policy_decisions (version_id, check_name, decision, reason_code,"
        " policy_version, service_ref) VALUES (%s, 'pii', 'allow', 'not_screened_by_policy',"
        " %s, 'none')",
        (job["version_id"], NOT_SCREENED_POLICY),
    )
    segments = _segments(conn, job["version_id"])
    if not segments:
        _unpublish(conn, job, "rejected", "not_indexed:empty_transcript")
        return "not_indexed"
    summary = _summary(conn, job["version_id"])
    item = MeetingInput(
        title=job["title"] or "",
        date=job["occurred_at"].isoformat(),
        organizer=job["organizer_email"] or "",
        attendees=tuple(_attendees(conn, job["communication_id"])),
        transcript=transcript_text(segments),
        provider_summary=summary,
    )
    analysis = _analyze(conn, backends.analyzer, item)
    _store_analysis(conn, job["version_id"], analysis)

    pieces = _chunks(job, segments, summary)
    vectors = backends.embedder.embed([p["embed"] for p in pieces]) if pieces else []
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (version_id, communication_id, chunk_kind, seq, segment_from,"
            " segment_to, start_ms, end_ms, speakers, display_text, embedding_input_sha256,"
            " embedding_model, embedding, source_ref)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            [
                (
                    job["version_id"],
                    job["communication_id"],
                    p["kind"],
                    p["seq"],
                    p["segment_from"],
                    p["segment_to"],
                    p["start_ms"],
                    p["end_ms"],
                    p["speakers"],
                    p["text"],
                    _sha(p["embed"]),
                    backends.embedder.model,
                    str(vector),
                    "fireflies" if p["kind"] == "provider_summary" else None,
                )
                for p, vector in zip(pieces, vectors, strict=True)
            ],
        )
    if conn.execute("SELECT ci.active_dataset() IS NULL AS paused").fetchone() == {"paused": True}:
        raise RuntimeError("mode_paused_before_publish")
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'superseded'"
        " WHERE communication_id = %s AND processing_state = 'published' AND id <> %s",
        (job["communication_id"], job["version_id"]),
    )
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'published', published_at = now()"
        " WHERE id = %s",
        (job["version_id"],),
    )
    conn.execute(
        "UPDATE communications SET active_version_id = %s WHERE id = %s",
        (job["version_id"], job["communication_id"]),
    )
    return "published"


def process_one(conn: Connection, backends: MeetingBackends) -> str | None:
    """Claim and process one meeting version; None when nothing is queued."""
    job = _claim(conn)
    if job is None:
        conn.rollback()
        return None
    try:
        # A savepoint keeps the row lock if the work fails, so no other worker
        # claims the version before it is marked failed.
        with conn.transaction():
            outcome = process_meeting(conn, job, backends)
        conn.commit()
        return outcome
    except Exception as exc:  # recorded as a content-free code; the run continues
        code = type(exc).__name__[:60]
        if isinstance(exc, AzureServiceError):  # service, status and code only
            code = f"{code}: {exc}"[:120]
        log.warning("meeting processing failed for version %s: %s", job["version_id"], code)
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'failed', last_error_code = %s"
            " WHERE id = %s",
            (code, job["version_id"]),
        )
        conn.commit()
        return "failed"


def process_meetings(
    connect: Callable[[], Connection],
    backends: MeetingBackends,
    *,
    limit: int | None = None,
    workers: int = 2,
    max_seconds: float | None = None,
) -> Counter[str]:
    return drain(
        connect,
        lambda conn: process_one(conn, backends),
        limit=limit,
        workers=workers,
        max_seconds=max_seconds,
    )


def retry_failed_meetings(conn: Connection) -> int:
    rows = conn.execute(
        "UPDATE communication_versions v SET processing_state = 'fetched', last_error_code = NULL"
        " FROM fireflies_meetings m WHERE m.communication_id = v.communication_id"
        " AND v.processing_state = 'failed' RETURNING v.id"
    ).fetchall()
    return len(rows)
