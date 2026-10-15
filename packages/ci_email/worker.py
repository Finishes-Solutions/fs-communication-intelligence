"""Job handlers for email: fetch + normalize + stage, withdraw, and process.

Each job re-checks the mode and the mailbox scope before reading the source
(jobs may have been admitted under an earlier revision). Participant
exclusions are evaluated per occurrence before any content is staged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from ci_core import jobs
from ci_core.blob import BlobStore
from ci_core.control import runtime_state
from ci_core.db import Connection, many, one, one_required, transaction
from ci_core.settings import Settings
from ci_email import attachments
from ci_email.capture import FETCH, PROCESS, WITHDRAW, capturable_scopes
from ci_email.graph import SKIPPED_FOLDERS, GraphClient, GraphError, GraphNotFound, GraphThrottled
from ci_email.normalize import (
    NORMALIZER_VERSION,
    EmailMessage,
    content_fingerprint,
    normalize_message,
    source_revision,
)
from ci_processing.chunking import CHUNKER_VERSION
from ci_processing.classifier import ClassifierInput
from ci_processing.labels import POLICY_VERSION
from ci_processing.pipeline import (
    ProcessingDeps,
    ProcessingInput,
    RetryableProcessingError,
    SourceHold,
    process,
)


@dataclass
class EmailWorker:
    conn: Connection
    graph: GraphClient
    blob: BlobStore
    settings: Settings
    deps: ProcessingDeps
    connection_id: str
    connection_ref: str
    worker_id: str = "local-worker"

    def profile(self) -> dict[str, Any]:
        """Everything that, when changed, makes reprocessing produce a new version."""
        return {
            "normalizer": NORMALIZER_VERSION,
            "chunker": CHUNKER_VERSION,
            "embedding_model": self.deps.embedder.model,
            "embedding_dimensions": self.deps.embedder.dimensions,
            "classifier": f"{self.deps.classifier.backend.value}:{self.deps.classifier.model}",
            "label_policy": POLICY_VERSION,
            "attachment_policy": self.settings.attachment_unsupported_policy.value,
        }


@dataclass
class JobOutcome:
    job_id: int
    job_type: str
    result: str  # content-free


@dataclass
class WorkerReport:
    outcomes: list[JobOutcome] = field(default_factory=list)
    skipped: str | None = None


def _retry_delay(attempts: int) -> int:
    return int(min(60, 2**attempts))


def run_until_idle(w: EmailWorker, *, max_jobs: int = 500) -> WorkerReport:
    report = WorkerReport()
    runtime = runtime_state(w.conn)
    if runtime is None or runtime.dataset_key is None:
        report.skipped = "mode_paused"  # jobs stay queued until the mode resumes
        return report
    jobs.recover_expired_leases(w.conn)
    for _ in range(max_jobs):
        claimed = jobs.claim(w.conn, w.worker_id)
        if not claimed:
            break
        job = claimed[0]
        report.outcomes.append(JobOutcome(job.id, job.job_type, _run_one(w, job)))
    return report


def _run_one(w: EmailWorker, job: jobs.Job) -> str:
    handler = {FETCH: handle_fetch, WITHDRAW: handle_withdraw, PROCESS: handle_process}.get(
        job.job_type
    )
    if handler is None:
        jobs.complete(w.conn, job, w.worker_id, state="cancelled", reason_code="unknown_job_type")
        return "cancelled:unknown_job_type"
    try:
        result = handler(w, job)
    except GraphThrottled as exc:
        state = jobs.fail(
            w.conn, job, w.worker_id, error_code=exc.code, retry_in_seconds=exc.retry_after_seconds
        )
        return f"{state}:{exc.code}"
    except RetryableProcessingError as exc:
        state = jobs.fail(
            w.conn,
            job,
            w.worker_id,
            error_code=exc.code,
            retry_in_seconds=_retry_delay(job.attempts),
        )
        return f"{state}:{exc.code}"
    except GraphError as exc:
        state = jobs.fail(
            w.conn,
            job,
            w.worker_id,
            error_code=exc.code,
            retry_in_seconds=_retry_delay(job.attempts),
        )
        return f"{state}:{exc.code}"
    if result.startswith("cancelled:"):
        jobs.complete(
            w.conn, job, w.worker_id, state="cancelled", reason_code=result.split(":", 1)[1]
        )
    else:
        jobs.complete(w.conn, job, w.worker_id)
    return result


def _scope_ok(w: EmailWorker, job: jobs.Job) -> str | None:
    runtime = runtime_state(w.conn)
    if runtime is None or runtime.dataset_key != job.dataset_key:
        return "cancelled:mode_changed"
    scope_ids = {str(s["id"]) for s in capturable_scopes(w.conn, w.connection_id, job.dataset_key)}
    if job.payload["scope_id"] not in scope_ids:
        return "cancelled:scope_not_capturable"
    return None


def _folder_well_known(w: EmailWorker, mailbox_id: str, folder_id: str | None) -> str | None:
    for f in w.graph.list_folders(mailbox_id):
        if f.id == folder_id:
            return f.well_known_name
    return None


def _participant_exclusion(conn: Connection, addresses: set[str]) -> str | None:
    if not addresses:
        return None
    domains = {a.split("@", 1)[1] for a in addresses if "@" in a}
    with transaction(conn, "ci_ingest"):
        row = one(
            conn,
            "SELECT exclusion_kind FROM exclusions WHERE revoked_at IS NULL AND"
            " ((exclusion_kind = 'participant_address' AND pattern = ANY(%s))"
            " OR (exclusion_kind = 'participant_domain' AND pattern = ANY(%s))) LIMIT 1",
            (sorted(addresses), sorted(domains)),
        )
    return None if row is None else f"{row['exclusion_kind']}_excluded"


def _audit(conn: Connection, action: str, target_id: str, details: dict[str, object]) -> None:
    conn.execute(
        "INSERT INTO audit_events (actor_type, actor_id, action, target_type, target_id, details)"
        " VALUES ('service', 'ingestion', %s, 'email_message', %s, %s)",
        (action, target_id, Jsonb(details)),
    )


def handle_fetch(w: EmailWorker, job: jobs.Job) -> str:
    blocked = _scope_ok(w, job)
    if blocked:
        return blocked
    p = job.payload
    try:
        raw = w.graph.get_message(p["mailbox_id"], p["message_id"])
    except GraphNotFound:
        return _withdraw(w, job, reason="deleted_at_source")
    wkn = _folder_well_known(w, p["mailbox_id"], raw.get("parentFolderId"))
    if wkn in SKIPPED_FOLDERS:
        return _withdraw(w, job, reason=f"moved_to_{wkn}")
    with transaction(w.conn, "ci_ingest"):
        mbx = one_required(
            w.conn,
            "SELECT primary_smtp_address FROM email_mailboxes WHERE scope_id = %s",
            (p["scope_id"],),
        )
    msg = normalize_message(
        raw,
        connection_ref=w.connection_ref,
        mailbox_id=p["mailbox_id"],
        mailbox_address=mbx["primary_smtp_address"],
    )
    if msg.is_draft:
        return "cancelled:draft_skipped"
    excluded = _participant_exclusion(w.conn, msg.participant_addresses)
    if excluded:
        with transaction(w.conn, "ci_ingest"):
            _audit(w.conn, "capture_skipped", p["message_id"], {"reason_code": excluded})
        return f"cancelled:{excluded}"

    revision = source_revision(msg)
    blob_key = f"raw/email/{job.dataset_key}/{p['mailbox_id']}/{p['message_id']}/{revision}.json"
    w.blob.put_json(blob_key, raw)  # stage before the database points at it
    raw_sha = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    return _stage(w, job, msg, revision, blob_key, raw_sha, len(json.dumps(raw)))


def _stage(
    w: EmailWorker,
    job: jobs.Job,
    msg: EmailMessage,
    revision: str,
    blob_key: str,
    raw_sha: str,
    raw_size: int,
) -> str:
    c = msg.communication
    p = job.payload
    profile = w.profile()
    with transaction(w.conn, "ci_ingest"):
        scope = one_required(
            w.conn, "SELECT corpus_id FROM source_scopes WHERE id = %s", (p["scope_id"],)
        )
        comm = one_required(
            w.conn,
            """
            INSERT INTO communications (dataset_key, connection_id, native_id, kind, corpus_id,
                                        title, occurred_at, source_url, provider_attributes)
            VALUES (%s, %s, %s, 'email', %s, %s, %s, %s, %s)
            ON CONFLICT (dataset_key, connection_id, native_id) DO UPDATE
            SET title = EXCLUDED.title, source_url = EXCLUDED.source_url,
                provider_attributes = EXCLUDED.provider_attributes, lifecycle_state = 'active',
                last_seen_at = now()
            RETURNING id
            """,
            (
                job.dataset_key,
                w.connection_id,
                c.native_id,
                scope["corpus_id"],
                c.title,
                c.occurred_at,
                c.source_url,
                Jsonb(c.provider_attributes),
            ),
        )
        comm_id = str(comm["id"])
        w.conn.execute(
            "INSERT INTO communication_scopes (communication_id, scope_id) VALUES (%s, %s)"
            " ON CONFLICT (communication_id, scope_id) DO UPDATE SET last_seen_at = now()",
            (comm_id, p["scope_id"]),
        )
        w.conn.execute(
            "DELETE FROM communication_participants WHERE communication_id = %s", (comm_id,)
        )
        for part in c.participants:
            prow = one_required(
                w.conn,
                """
                INSERT INTO participants (provider, provider_identity, email_normalized,
                                          display_name)
                VALUES ('email', %s, %s, %s)
                ON CONFLICT (provider, provider_identity) DO UPDATE
                SET display_name = COALESCE(EXCLUDED.display_name, participants.display_name)
                RETURNING id
                """,
                (part.address, part.address, part.display_name),
            )
            w.conn.execute(
                "INSERT INTO communication_participants (communication_id, participant_id, role)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (comm_id, prow["id"], part.role.value),
            )
        fingerprint = content_fingerprint(msg)
        w.conn.execute(
            """
            INSERT INTO email_messages (communication_id, mailbox_scope_id, internet_message_id,
                                        conversation_id, change_key, received_at, sent_at,
                                        direction, parent_folder_id, has_attachments,
                                        content_fingerprint)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (communication_id) DO UPDATE
            SET change_key = EXCLUDED.change_key, parent_folder_id = EXCLUDED.parent_folder_id,
                has_attachments = EXCLUDED.has_attachments,
                content_fingerprint = EXCLUDED.content_fingerprint, updated_at = now()
            """,
            (
                comm_id,
                p["scope_id"],
                c.internet_message_id,
                msg.conversation_id,
                msg.change_key,
                msg.received_at,
                msg.sent_at,
                c.direction.value,
                msg.parent_folder_id,
                bool(c.attachments),
                fingerprint,
            ),
        )
        if fingerprint and c.internet_message_id:
            for other in many(
                w.conn,
                """
                SELECT em.communication_id FROM email_messages em
                JOIN communications oc ON oc.id = em.communication_id
                WHERE em.internet_message_id = %s AND em.content_fingerprint = %s
                  AND oc.dataset_key = %s AND em.communication_id <> %s
                """,
                (c.internet_message_id, fingerprint, job.dataset_key, comm_id),
            ):
                w.conn.execute(
                    "INSERT INTO relationships (from_communication_id, to_communication_id, kind,"
                    " provenance) VALUES (%s, %s, 'possible_duplicate', %s) ON CONFLICT DO NOTHING",
                    (
                        comm_id,
                        other["communication_id"],
                        Jsonb({"basis": "internet_message_id+content_fingerprint", "fp": "fp.v1"}),
                    ),
                )

        existing = one(
            w.conn,
            "SELECT id, processing_state FROM communication_versions"
            " WHERE communication_id = %s AND source_revision = %s AND profile = %s",
            (comm_id, revision, Jsonb(profile)),
        )
        if existing is not None:
            version_id, state = str(existing["id"]), str(existing["processing_state"])
            if state in ("published", "held", "rejected", "superseded", "withdrawn"):
                return f"unchanged:{state}"
        else:
            vrow = one_required(
                w.conn,
                """
                INSERT INTO communication_versions (communication_id, version_no, source_revision,
                                                    raw_blob_key, profile, processing_state)
                VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1 FROM communication_versions
                             WHERE communication_id = %s), %s, %s, %s, 'normalized')
                RETURNING id
                """,
                (comm_id, comm_id, revision, blob_key, Jsonb(profile)),
            )
            version_id = str(vrow["id"])
            w.conn.execute(
                "INSERT INTO artifacts (version_id, kind, blob_key, mime_type, sha256, size_bytes)"
                " VALUES (%s, 'raw_payload', %s, 'application/json', %s, %s)",
                (version_id, blob_key, raw_sha, raw_size),
            )
            for a in c.attachments:
                w.conn.execute(
                    "INSERT INTO email_attachments (version_id, native_attachment_id, name,"
                    " content_type, size_bytes, is_inline, content_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        version_id,
                        a.native_id,
                        a.name,
                        a.content_type,
                        a.size_bytes,
                        a.is_inline,
                        a.content_id,
                    ),
                )
        jobs.admit(
            w.conn,
            dataset_key=job.dataset_key,
            config_revision=job.config_revision,
            job_type=PROCESS,
            dedupe_key=version_id,
            payload={"version_id": version_id, "scope_id": p["scope_id"]},
        )
    return "staged"


def handle_withdraw(w: EmailWorker, job: jobs.Job) -> str:
    """A delta @removed: deleted, or moved out of this folder. Check before withdrawing."""
    p = job.payload
    try:
        raw = w.graph.get_message(p["mailbox_id"], p["message_id"])
    except GraphNotFound:
        return _withdraw(w, job, reason="deleted_at_source")
    wkn = _folder_well_known(w, p["mailbox_id"], raw.get("parentFolderId"))
    if wkn in SKIPPED_FOLDERS:
        return _withdraw(w, job, reason=f"moved_to_{wkn}")
    with transaction(w.conn, "ci_ingest"):
        w.conn.execute(
            "UPDATE email_messages em SET parent_folder_id = %s, updated_at = now()"
            " FROM communications c WHERE c.id = em.communication_id AND c.dataset_key = %s"
            " AND c.connection_id = %s AND c.native_id = %s",
            (raw.get("parentFolderId"), job.dataset_key, w.connection_id, p["message_id"]),
        )
    return "moved_within_scope"


def _withdraw(w: EmailWorker, job: jobs.Job, *, reason: str) -> str:
    """Retrieval is blocked first (lifecycle state); purge is a later maintenance step."""
    with transaction(w.conn, "ci_ingest"):
        row = one(
            w.conn,
            "UPDATE communications SET lifecycle_state = 'deleted_at_source', last_seen_at = now()"
            " WHERE dataset_key = %s AND connection_id = %s AND native_id = %s RETURNING id",
            (job.dataset_key, w.connection_id, job.payload["message_id"]),
        )
        if row is not None:
            _audit(w.conn, "withdrawn", job.payload["message_id"], {"reason_code": reason})
    return f"withdrawn:{reason}" if row is not None else f"cancelled:not_captured_{reason}"


def handle_process(w: EmailWorker, job: jobs.Job) -> str:
    runtime = runtime_state(w.conn)
    if runtime is None or runtime.dataset_key != job.dataset_key:
        return "cancelled:mode_changed"
    version_id = job.payload["version_id"]
    with transaction(w.conn, "ci_ingest"):
        row = one(
            w.conn,
            """
            SELECT v.raw_blob_key, v.communication_id, c.native_id, m.primary_smtp_address,
                   s.native_scope_id
            FROM communication_versions v
            JOIN communications c ON c.id = v.communication_id
            JOIN email_messages em ON em.communication_id = c.id
            JOIN email_mailboxes m ON m.scope_id = em.mailbox_scope_id
            JOIN source_scopes s ON s.id = m.scope_id
            WHERE v.id = %s
            """,
            (version_id,),
        )
    if row is None:
        return "cancelled:unknown_version"
    raw = w.blob.get_json(row["raw_blob_key"])
    msg = normalize_message(
        raw,
        connection_ref=w.connection_ref,
        mailbox_id=row["native_scope_id"],
        mailbox_address=row["primary_smtp_address"],
    )
    c = msg.communication

    verdict = attachments.evaluate(c.attachments, w.settings.attachment_unsupported_policy)
    with transaction(w.conn, "ci_ingest"):
        for att_id, status in verdict.statuses.items():
            w.conn.execute(
                "UPDATE email_attachments SET status = %s"
                " WHERE version_id = %s AND native_attachment_id = %s",
                (status, version_id, att_id),
            )
    checks: tuple[SourceHold, ...] = ()
    if c.attachments:
        checks = (
            SourceHold(
                "attachment_policy",
                "hold" if verdict.hold_reason else "allow",
                verdict.hold_reason or ("partial_coverage" if verdict.partial else "exempt_inline"),
                f"d-e1:{w.settings.attachment_unsupported_policy.value}",
            ),
        )
    header = (
        f"Subject: {c.title or ''}\nFrom: {msg.from_address or ''}\n"
        f"Date: {c.occurred_at:%Y-%m-%d}\nMailbox: {msg.mailbox_address}"
    )
    item = ProcessingInput(
        version_id=version_id,
        communication_id=str(row["communication_id"]),
        text=c.body_text or "",
        classifier_input=ClassifierInput(
            c.title or "", c.body_text or "", msg.from_address, c.direction.value
        ),
        embedding_header=header,
        source_checks=checks,
    )
    return process(w.conn, item, w.deps, expected_dataset=job.dataset_key)
