"""Process captured email versions: extract, screen, classify, chunk, embed, publish.

Workers claim ``fetched`` versions with ``FOR UPDATE SKIP LOCKED`` and finish
each one in a single transaction, so a crash leaves the version ``fetched``
and another run picks it up. Publication follows docs/DATABASE.md section 4:
the new version becomes ``published``, the prior one ``superseded``, and
``communications.active_version_id`` moves, all at once. A version that is not
published also withdraws any previously published version of the same
message.

Screening runs before classification; restricted and held content is never
classified by AI, embedded or published. Classification of identical content
is reused across mailbox copies. Logs carry ids, counts and codes only.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from ci_core.db import Connection, required
from ci_email.graph import GraphClient, GraphError
from ci_processing.chunking import chunk_text
from ci_processing.classification import (
    LABELS,
    POLICY_VERSION,
    ClassificationInput,
    ClassificationResult,
    Classifier,
)
from ci_processing.embedding import Embedder
from ci_processing.extraction import Extraction, ExtractionStatus, extract
from ci_processing.originals import OriginalStore, OriginalStoreError
from ci_processing.policy import Disposition, Outcome, classification_outcome, screening_gate
from ci_processing.screening import Screener

log = logging.getLogger(__name__)

BODY_CAP = 30_000  # characters screened and indexed from a body
ATTACHMENT_CAP = 60_000  # per attachment
CLASSIFY_BODY_CHARS = 12_000
CLASSIFY_ATTACHMENT_CHARS = 3_000
CLASSIFY_MAX_ATTACHMENTS = 5


@dataclass
class Backends:
    screener: Screener
    classifier: Classifier
    embedder: Embedder
    graph: GraphClient | None = None
    extraction_enabled: bool = False
    attachment_max_bytes: int = 25 * 1024 * 1024
    originals: OriginalStore | None = None  # keep original files (CLAUDE.md section 3a)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _claim(conn: Connection) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT v.id::text AS version_id, c.id::text AS communication_id, c.native_id,
               c.title, c.occurred_at, s.native_scope_id AS mailbox_native_id,
               m.primary_smtp_address AS mailbox_address
        FROM communication_versions v
        JOIN communications c ON c.id = v.communication_id
        JOIN email_messages em ON em.communication_id = c.id
        JOIN source_scopes s ON s.id = em.mailbox_scope_id
        JOIN email_mailboxes m ON m.scope_id = s.id
        WHERE v.processing_state = 'fetched' AND c.dataset_key = ci.active_dataset()
        ORDER BY v.created_at
        FOR UPDATE OF v SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()


def _extract_attachments(conn: Connection, job: dict[str, Any], backends: Backends) -> None:
    rows = conn.execute(
        "SELECT native_attachment_id, name, content_type, size_bytes, is_inline"
        " FROM email_attachments WHERE version_id = %s AND extraction_status = 'not_attempted'",
        (job["version_id"],),
    ).fetchall()
    for a in rows:
        result: Extraction
        digest: str | None = None
        stored = False
        if a["is_inline"] and (a["content_type"] or "").startswith("image/"):
            result = Extraction(ExtractionStatus.UNSUPPORTED)  # signature images; no OCR yet
        elif a["size_bytes"] > backends.attachment_max_bytes:
            result = Extraction(ExtractionStatus.TOO_LARGE)
        else:
            assert backends.graph is not None  # noqa: S101 - guarded by extraction_enabled
            try:
                data = backends.graph.get_attachment_bytes(
                    job["mailbox_native_id"], job["native_id"], a["native_attachment_id"]
                )
            except GraphError:
                result = Extraction(ExtractionStatus.FAILED)
            else:
                digest = hashlib.sha256(data).hexdigest()
                stored = _store_original(conn, backends, digest, data, a["content_type"])
                reused = conn.execute(
                    "SELECT a.extraction_status, t.content FROM email_attachments a"
                    " LEFT JOIN version_texts t ON t.version_id = a.version_id"
                    "  AND t.text_kind = 'attachment' AND t.ref = a.native_attachment_id"
                    " WHERE a.sha256 = %s AND a.extraction_status IN"
                    "  ('extracted', 'no_text', 'unsupported') LIMIT 1",
                    (digest,),
                ).fetchone()
                if reused:
                    result = Extraction(
                        ExtractionStatus(reused["extraction_status"]), reused["content"] or ""
                    )
                else:
                    result = extract(
                        a["name"], a["content_type"], data, max_bytes=backends.attachment_max_bytes
                    )
        conn.execute(
            "UPDATE email_attachments SET extraction_status = %s, sha256 = %s, extractor = %s,"
            " extracted_chars = %s, original_stored_at = CASE WHEN %s THEN now() END"
            " WHERE version_id = %s AND native_attachment_id = %s",
            (
                result.status.value,
                digest,
                "extract-v1-no-ocr",
                len(result.text) or None,
                stored,
                job["version_id"],
                a["native_attachment_id"],
            ),
        )
        if result.status is ExtractionStatus.EXTRACTED and result.text:
            text = result.text[:ATTACHMENT_CAP]
            conn.execute(
                "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256, truncated)"
                " VALUES (%s, 'attachment', %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    job["version_id"],
                    a["native_attachment_id"],
                    text,
                    _sha(text),
                    result.truncated or len(result.text) > ATTACHMENT_CAP,
                ),
            )


def _store_original(
    conn: Connection, backends: Backends, digest: str, data: bytes, content_type: str | None
) -> bool:
    """Keep the original file once per distinct content. A store failure never fails the
    message: the file is retried by :func:`store_missing_originals`."""
    if backends.originals is None:
        return False
    if conn.execute(
        "SELECT 1 FROM email_attachments WHERE sha256 = %s AND original_stored_at IS NOT NULL"
        " LIMIT 1",
        (digest,),
    ).fetchone():
        return True
    try:
        backends.originals.put(digest, data, content_type)
    except (OriginalStoreError, httpx.HTTPError) as exc:
        log.warning("original not stored: %s", type(exc).__name__)
        return False
    return True


def store_missing_originals(
    connect: Callable[[], Connection], backends: Backends, limit: int = 100
) -> Counter[str]:
    """Store originals for published messages processed before storage was on (or
    whose upload failed). Bounded per call; inline images and oversized files are skipped."""
    counts: Counter[str] = Counter()
    if backends.originals is None or backends.graph is None:
        return counts
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.version_id::text, a.native_attachment_id, a.content_type, a.sha256,
                   c.native_id, s.native_scope_id
            FROM email_attachments a
            JOIN communication_versions v ON v.id = a.version_id
            JOIN communications c ON c.id = v.communication_id
            JOIN email_messages em ON em.communication_id = c.id
            JOIN source_scopes s ON s.id = em.mailbox_scope_id
            WHERE a.original_stored_at IS NULL AND v.processing_state = 'published'
              AND a.extraction_status <> 'not_attempted' AND a.size_bytes <= %s
              AND NOT (a.is_inline AND coalesce(a.content_type, '') LIKE 'image/%%')
            ORDER BY random() LIMIT %s
            """,
            (backends.attachment_max_bytes, limit),
        ).fetchall()
        conn.commit()
        for r in rows:
            known = (
                r["sha256"]
                and conn.execute(
                    "SELECT 1 FROM email_attachments WHERE sha256 = %s"
                    " AND original_stored_at IS NOT NULL LIMIT 1",
                    (r["sha256"],),
                ).fetchone()
            )
            if not known:
                try:
                    data = backends.graph.get_attachment_bytes(
                        r["native_scope_id"], r["native_id"], r["native_attachment_id"]
                    )
                except GraphError:
                    counts["original_download_failed"] += 1
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if not _store_original(conn, backends, digest, data, r["content_type"]):
                    counts["original_store_failed"] += 1
                    continue
                conn.execute(
                    "UPDATE email_attachments SET sha256 = %s WHERE version_id = %s"
                    " AND native_attachment_id = %s",
                    (digest, r["version_id"], r["native_attachment_id"]),
                )
            conn.execute(
                "UPDATE email_attachments SET original_stored_at = now() WHERE version_id = %s"
                " AND native_attachment_id = %s",
                (r["version_id"], r["native_attachment_id"]),
            )
            conn.commit()
            counts["originals_backfilled"] += 1
    return counts


def _texts(conn: Connection, version_id: str) -> tuple[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT t.text_kind, t.ref, t.content, a.name FROM version_texts t"
        " LEFT JOIN email_attachments a ON a.version_id = t.version_id"
        "  AND a.native_attachment_id = t.ref"
        " WHERE t.version_id = %s ORDER BY t.text_kind DESC, t.ref",
        (version_id,),
    ).fetchall()
    body = next((r["content"] for r in rows if r["text_kind"] == "email_body"), "")
    return body[:BODY_CAP], [r for r in rows if r["text_kind"] == "attachment"]


def _participants(conn: Connection, communication_id: str) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT cp.role, p.email_normalized FROM communication_participants cp"
        " JOIN participants p ON p.id = cp.participant_id WHERE cp.communication_id = %s"
        " ORDER BY p.email_normalized",
        (communication_id,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["role"], []).append(r["email_normalized"])
    return out


def _classify(
    conn: Connection, backends: Backends, item: ClassificationInput
) -> tuple[ClassificationResult, int | None]:
    model = getattr(backends.classifier, "model", type(backends.classifier).__name__)
    prior = conn.execute(
        "SELECT id, summary, action_text, action_for, due_text, risk_text FROM classification_runs"
        " WHERE input_sha256 = %s AND model = %s AND policy_version = %s AND reused_from IS NULL"
        " ORDER BY id LIMIT 1",
        (item.sha256(), model, POLICY_VERSION),
    ).fetchone()
    if prior:
        labels = conn.execute(
            "SELECT label, probability FROM classification_labels WHERE run_id = %s",
            (prior["id"],),
        ).fetchall()
        return ClassificationResult(
            model,
            POLICY_VERSION,
            item.sha256(),
            {r["label"]: r["probability"] for r in labels},
            prior["summary"],
            prior["action_text"],
            tuple(prior["action_for"]),
            prior["due_text"],
            prior["risk_text"],
        ), int(prior["id"])
    return backends.classifier.classify(item), None


def _store_classification(
    conn: Connection,
    version_id: str,
    result: ClassificationResult,
    outcome: Outcome,
    reused_from: int | None,
) -> None:
    run_id = required(
        conn.execute(
            "INSERT INTO classification_runs (version_id, model, policy_version, input_sha256,"
            " disposition, reason_codes, summary, action_text, action_for, due_text, risk_text,"
            " reused_from) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (version_id, model, policy_version) DO NOTHING RETURNING id",
            (
                version_id,
                result.model,
                result.policy_version,
                result.input_sha256,
                outcome.disposition.value,
                list(outcome.reason_codes),
                result.summary,
                result.action_text,
                list(result.action_for),
                result.due_text,
                result.risk_text,
                reused_from,
            ),
        ).fetchone()
    )["id"]
    decisions = result.decisions()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO classification_labels (run_id, label, probability, decision)"
            " VALUES (%s, %s, %s, %s)",
            [
                (run_id, lb.name, result.probabilities.get(lb.name), decisions[lb.name].value)
                for lb in LABELS
            ],
        )


def _chunks(
    job: dict[str, Any], sender: str, body: str, attachments: list[dict[str, Any]]
) -> list[tuple[str, str | None, int, str, str]]:
    """(kind, source_ref, seq, display_text, embedding_input) for every chunk."""
    header = (
        f"Subject: {job['title'] or ''}\nFrom: {sender}\n"
        f"Date: {job['occurred_at']:%Y-%m-%d}\nMailbox: {job['mailbox_address']}\n"
    )
    out: list[tuple[str, str | None, int, str, str]] = []
    for seq, text in enumerate(chunk_text(body)):
        out.append(("email_body", None, seq, text, f"{header}\n{text}"))
    for a in attachments:
        for seq, text in enumerate(chunk_text(a["content"])):
            out.append(
                (
                    "attachment_text",
                    a["ref"],
                    seq,
                    text,
                    f"{header}Attachment: {a['name'] or ''}\n\n{text}",
                )
            )
    return out


def _withdraw_prior(conn: Connection, job: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'superseded'"
        " WHERE communication_id = %s AND processing_state = 'published' AND id <> %s",
        (job["communication_id"], job["version_id"]),
    )


def process_version(conn: Connection, job: dict[str, Any], backends: Backends) -> Disposition:
    if backends.extraction_enabled and backends.graph is not None:
        _extract_attachments(conn, job, backends)
    body, attachments = _texts(conn, job["version_id"])
    screen_text = "\n\n".join([job["title"] or "", body, *(a["content"] for a in attachments)])
    screening = backends.screener.screen(screen_text)
    for check in screening.checks:
        conn.execute(
            "INSERT INTO policy_decisions (version_id, check_name, decision, reason_code,"
            " policy_version, service_ref) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                job["version_id"],
                check.check_name,
                check.decision.value,
                check.reason_code,
                POLICY_VERSION,
                check.service_ref,
            ),
        )
    outcome = screening_gate(screening)
    people = _participants(conn, job["communication_id"])
    sender = (people.get("from") or [""])[0]
    if outcome is None:
        item = ClassificationInput(
            subject=job["title"] or "",
            body_text=body[:CLASSIFY_BODY_CHARS],
            sender=sender,
            to=tuple(people.get("to", [])),
            cc=tuple(people.get("cc", [])),
            sent=job["occurred_at"].isoformat(),
            attachments=tuple(
                (a["name"] or "", a["content"][:CLASSIFY_ATTACHMENT_CHARS])
                for a in attachments[:CLASSIFY_MAX_ATTACHMENTS]
            ),
        )
        result, reused_from = _classify(conn, backends, item)
        outcome = classification_outcome(result)
        _store_classification(conn, job["version_id"], result, outcome, reused_from)

    if not outcome.disposition.embedded:
        state = "held" if outcome.disposition is Disposition.HELD else "rejected"
        reason = f"{outcome.disposition.value}:{','.join(outcome.reason_codes)}"
        _withdraw_prior(conn, job)
        conn.execute(
            "UPDATE communication_versions SET processing_state = %s, hold_reason_code = %s"
            " WHERE id = %s",
            (state, reason, job["version_id"]),
        )
        conn.execute(
            "UPDATE communications SET active_version_id = NULL WHERE id = %s",
            (job["communication_id"],),
        )
        return outcome.disposition

    pieces = _chunks(job, sender, body, attachments)
    vectors = backends.embedder.embed([p[4] for p in pieces]) if pieces else []
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO chunks (version_id, communication_id, chunk_kind, seq, display_text,"
            " embedding_input_sha256, embedding_model, embedding, source_ref)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            [
                (
                    job["version_id"],
                    job["communication_id"],
                    kind,
                    seq,
                    text,
                    _sha(emb_input),
                    backends.embedder.model,
                    str(vector),
                    ref,
                )
                for (kind, ref, seq, text, emb_input), vector in zip(pieces, vectors, strict=True)
            ],
        )
    if conn.execute("SELECT ci.active_dataset() IS NULL AS paused").fetchone() == {"paused": True}:
        raise RuntimeError("mode_paused_before_publish")
    _withdraw_prior(conn, job)
    conn.execute(
        "UPDATE communication_versions SET processing_state = 'published', published_at = now()"
        " WHERE id = %s",
        (job["version_id"],),
    )
    conn.execute(
        "UPDATE communications SET active_version_id = %s WHERE id = %s",
        (job["version_id"], job["communication_id"]),
    )
    return outcome.disposition


def process_one(conn: Connection, backends: Backends) -> str | None:
    """Claim and process one version; returns its outcome, or None when nothing is queued."""
    job = _claim(conn)
    if job is None:
        conn.rollback()
        return None
    try:
        # A savepoint: on failure the work is undone but the row lock is kept,
        # so no other worker can claim the version before it is marked failed.
        with conn.transaction():
            disposition = process_version(conn, job, backends)
        conn.commit()
        return disposition.value
    except Exception as exc:  # recorded as a content-free code, then the run continues
        code = type(exc).__name__[:60]
        log.warning("processing failed for version %s: %s", job["version_id"], code)
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'failed', last_error_code = %s"
            " WHERE id = %s",
            (code, job["version_id"]),
        )
        conn.commit()
        return "failed"


def process_all(
    connect: Callable[[], Connection],
    backends: Backends,
    *,
    limit: int | None = None,
    workers: int = 4,
    max_seconds: float | None = None,
) -> Counter[str]:
    """Process until nothing is queued, ``limit`` versions are done, or ``max_seconds``
    pass (checked before each claim, so a run can overrun by one message)."""
    counts: Counter[str] = Counter()
    budget = [limit if limit is not None else 10**12]
    lock = threading.Lock()
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None

    def take() -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        with lock:
            budget[0] -= 1
            return budget[0] >= 0

    def worker() -> Counter[str]:
        local: Counter[str] = Counter()
        with connect() as conn:
            while take():
                outcome = process_one(conn, backends)
                if outcome is None:
                    break
                local[outcome] += 1
        return local

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(lambda _: worker(), range(max(1, workers))):
            counts.update(result)
    return counts


def retry_failed(conn: Connection) -> int:
    rows = conn.execute(
        "UPDATE communication_versions SET processing_state = 'fetched', last_error_code = NULL"
        " WHERE processing_state = 'failed' RETURNING id"
    ).fetchall()
    return len(rows)
