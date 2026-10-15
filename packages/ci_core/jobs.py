"""Durable job queue in PostgreSQL (docs/DATABASE.md section 5).

Admission collapses duplicates on ``jobs_open_dedupe``. Claiming uses
``FOR UPDATE SKIP LOCKED`` with a lease; completion only succeeds for the
current lease owner, so a worker that lost its lease cannot overwrite another
worker's result. Payloads carry identifiers only, never content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from ci_core.db import Connection, many, one, transaction


@dataclass(frozen=True)
class Job:
    id: int
    dataset_key: str
    config_revision: int
    job_type: str
    dedupe_key: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def admit(
    conn: Connection,
    *,
    dataset_key: str,
    config_revision: int,
    job_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
    priority: int = 100,
    delay_seconds: int = 0,
) -> int | None:
    """Queue a job. Returns its id, or None when an open duplicate already exists.

    Runs inside the caller's ``ci_ingest`` transaction.
    """
    row = one(
        conn,
        """
        INSERT INTO ci.jobs (dataset_key, config_revision, job_type, dedupe_key, payload,
                             priority, run_after)
        VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            dataset_key,
            config_revision,
            job_type,
            dedupe_key,
            Jsonb(payload),
            priority,
            delay_seconds,
        ),
    )
    return None if row is None else int(row["id"])


def claim(conn: Connection, worker: str, *, limit: int = 1, lease_seconds: int = 300) -> list[Job]:
    with transaction(conn, "ci_ingest"):
        rows = many(
            conn,
            """
            WITH next AS (
              SELECT id FROM ci.jobs
              WHERE state = 'queued' AND run_after <= now()
              ORDER BY priority, run_after, id
              FOR UPDATE SKIP LOCKED LIMIT %s)
            UPDATE ci.jobs j SET state = 'leased', lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   attempts = attempts + 1, updated_at = now()
            FROM next WHERE j.id = next.id
            RETURNING j.id, j.dataset_key, j.config_revision, j.job_type, j.dedupe_key,
                      j.payload, j.attempts, j.max_attempts
            """,
            (limit, worker, lease_seconds),
        )
    return [Job(**r) for r in rows]


def complete(
    conn: Connection,
    job: Job,
    worker: str,
    *,
    state: str = "succeeded",
    reason_code: str | None = None,
) -> bool:
    """Finish a leased job as 'succeeded' or 'cancelled'. False if the lease was lost."""
    if state not in ("succeeded", "cancelled"):
        raise ValueError(f"cannot complete a job as {state}")
    with transaction(conn, "ci_ingest"):
        row = one(
            conn,
            """
            UPDATE ci.jobs SET state = %s, last_error_code = %s, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = now()
            WHERE id = %s AND lease_owner = %s AND state = 'leased'
            RETURNING id
            """,
            (state, reason_code, job.id, worker),
        )
    return row is not None


def fail(
    conn: Connection, job: Job, worker: str, *, error_code: str, retry_in_seconds: int
) -> str | None:
    """Record a failed attempt: requeue with a delay, or 'dead' when attempts run out.

    Returns the new state, or None if the lease was lost.
    """
    with transaction(conn, "ci_ingest"):
        row = one(
            conn,
            """
            UPDATE ci.jobs SET
                   state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                   run_after = now() + make_interval(secs => %s),
                   last_error_code = %s, lease_owner = NULL, lease_expires_at = NULL,
                   updated_at = now()
            WHERE id = %s AND lease_owner = %s AND state = 'leased'
            RETURNING state
            """,
            (retry_in_seconds, error_code, job.id, worker),
        )
    return None if row is None else str(row["state"])


def recover_expired_leases(conn: Connection) -> int:
    with transaction(conn, "ci_ingest"):
        rows = many(
            conn,
            """
            UPDATE ci.jobs
            SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
                lease_owner = NULL, lease_expires_at = NULL, run_after = now(),
                updated_at = now()
            WHERE state = 'leased' AND lease_expires_at < now()
            RETURNING id
            """,
        )
    return len(rows)
