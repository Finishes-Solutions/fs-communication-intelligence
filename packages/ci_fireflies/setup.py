"""Operator setup and the schedule for Fireflies capture.

One ``fireflies_workspace`` scope stands for the whole Fireflies team (Matthew,
2026-10-28: "pull Fireflies meetings from the whole company"). People are kept
out with participant exclusions (``ci scope exclude --participant``), single
meetings with item exclusions. Functions do not commit; callers own the
transaction. Every change is audited.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from ci_core.db import Connection, required
from ci_core.operator import OperatorError, audit, set_provider
from ci_core.runtime import EmptyScopeError, active_config

CORPUS_KEY = "meetings-enrolled"
READER_ALL_ROLE = "Communications.Reader.All"
SCOPE_KIND = "fireflies_workspace"
SCOPE_POLICY_VERSION = "fireflies-scope-v1-whole-workspace"
INTERVALS = (15, 30, 60, 120, 240, 720, 1440)
MAX_LOOKBACK_HOURS = 744
MAX_REQUEST_HOURS = 2232  # 93 days, like a requested email backfill
RUNNING_GRACE = timedelta(minutes=130)


@dataclass(frozen=True)
class Workspace:
    connection_id: str
    corpus_id: str
    scope_id: str


def init_workspace(
    conn: Connection, workspace_ref: str, actor: str, *, production: bool = False
) -> Workspace:
    """Register the workspace, its corpus and reader grant, approve the workspace scope
    for test (and production only when asked), and enable the source. Safe to rerun."""
    connection = required(
        conn.execute(
            "INSERT INTO source_connections (provider, account_ref, display_name, enabled)"
            " VALUES ('fireflies', %s, 'Fireflies workspace', true)"
            " ON CONFLICT (provider, account_ref) DO UPDATE SET enabled = true RETURNING id::text",
            (workspace_ref,),
        ).fetchone()
    )["id"]
    corpus = required(
        conn.execute(
            "INSERT INTO corpora (corpus_key, description)"
            " VALUES (%s, 'Fireflies meetings from the whole workspace')"
            " ON CONFLICT (corpus_key) DO UPDATE SET corpus_key = EXCLUDED.corpus_key"
            " RETURNING id::text",
            (CORPUS_KEY,),
        ).fetchone()
    )["id"]
    if not conn.execute(
        "SELECT 1 FROM access_grants WHERE corpus_id = %s AND principal_type = 'entra_app_role'"
        " AND principal_id = %s AND revoked_at IS NULL",
        (corpus, READER_ALL_ROLE),
    ).fetchone():
        conn.execute(
            "INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)"
            " VALUES (%s, 'entra_app_role', %s, %s)",
            (corpus, READER_ALL_ROLE, actor),
        )
    scope = required(
        conn.execute(
            "INSERT INTO source_scopes (connection_id, scope_kind, native_scope_id, display_label,"
            " corpus_id, scope_state, state_checked_at, test_approved, production_approved,"
            " policy_version, approved_by, approved_at)"
            " VALUES (%s, %s, %s, 'Fireflies workspace (all members)', %s, 'enrolled', now(),"
            " true, %s, %s, %s, now())"
            " ON CONFLICT (connection_id, scope_kind, native_scope_id) DO UPDATE SET"
            " scope_state = 'enrolled', test_approved = true,"
            " production_approved = source_scopes.production_approved"
            "  OR EXCLUDED.production_approved,"
            " approved_by = EXCLUDED.approved_by, approved_at = now()"
            " RETURNING id::text",
            (
                connection,
                SCOPE_KIND,
                workspace_ref,
                corpus,
                production,
                SCOPE_POLICY_VERSION,
                actor,
            ),
        ).fetchone()
    )["id"]
    set_provider(conn, "fireflies", True, actor)
    audit(
        conn,
        actor,
        "fireflies_init",
        "source_scope",
        scope,
        {"production_approved": production},
    )
    return Workspace(connection, corpus, scope)


def eligible_workspace(conn: Connection) -> Workspace:
    """The workspace capture may read under the active revision. Fails closed."""
    config = active_config(conn)
    if config.dataset is None:
        raise EmptyScopeError("mode paused; Fireflies capture refuses to run")
    if "fireflies" not in config.enabled_providers:
        raise EmptyScopeError("the fireflies source is not enabled; run `ci fireflies init`")
    approved = "test_approved" if config.dataset == "test" else "production_approved"
    row = conn.execute(
        f"""
        SELECT s.connection_id::text AS connection_id, s.corpus_id::text AS corpus_id,
               s.id::text AS scope_id
        FROM source_scopes s
        JOIN source_connections c ON c.id = s.connection_id AND c.enabled
                                 AND c.provider = 'fireflies'
        WHERE s.scope_kind = 'fireflies_workspace' AND s.scope_state = 'enrolled' AND s.{approved}
          AND NOT EXISTS (SELECT 1 FROM exclusions e
                          WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL
                            AND e.scope_kind = s.scope_kind
                            AND e.native_scope_id = s.native_scope_id)
        ORDER BY s.native_scope_id LIMIT 1
        """  # noqa: S608 - the column name is one of two literals above
    ).fetchone()
    if row is None:
        raise EmptyScopeError(
            f"mode {config.mode} has no approved Fireflies workspace; capture refuses to run"
        )
    return Workspace(**row)


# --- schedule (ci.fireflies_settings) --------------------------------------


@dataclass(frozen=True)
class Schedule:
    capture_enabled: bool
    capture_interval_min: int
    lookback_hours: float
    processing_enabled: bool
    run_requested_at: datetime | None
    run_requested_hours: float | None
    last_capture_started_at: datetime | None
    updated_at: datetime
    updated_by: str

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in data.items()}


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str
    trigger: str = "schedule"
    hours: float = 0.0


def read_schedule(conn: Connection) -> Schedule:
    row = required(conn.execute("SELECT * FROM fireflies_settings WHERE id").fetchone())
    row.pop("id")
    row["lookback_hours"] = float(row["lookback_hours"])
    if row["run_requested_hours"] is not None:
        row["run_requested_hours"] = float(row["run_requested_hours"])
    return Schedule(**row)


def capture_decision(schedule: Schedule, now: datetime) -> Decision:
    """A pending "run now" request always wins; otherwise every interval, around the clock
    (meetings end at all hours and transcripts arrive later)."""
    if schedule.run_requested_at is not None:
        return Decision(
            True, "run requested", "manual", schedule.run_requested_hours or schedule.lookback_hours
        )
    if not schedule.capture_enabled:
        return Decision(False, "capture is turned off")
    last = schedule.last_capture_started_at
    if last is not None and now - last < timedelta(minutes=schedule.capture_interval_min - 1):
        return Decision(False, "not due yet")
    return Decision(True, "due", "schedule", schedule.lookback_hours)


_EDITABLE = {"capture_enabled", "capture_interval_min", "lookback_hours", "processing_enabled"}


def update_schedule(conn: Connection, actor: str, **changes: Any) -> Schedule:
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise OperatorError(f"unknown schedule fields: {', '.join(sorted(unknown))}")
    current = read_schedule(conn)
    interval = changes.get("capture_interval_min", current.capture_interval_min)
    lookback = float(changes.get("lookback_hours", current.lookback_hours))
    if interval not in INTERVALS:
        raise OperatorError(f"interval must be one of {INTERVALS} minutes")
    if not 0 < lookback <= MAX_LOOKBACK_HOURS:
        raise OperatorError(f"look-back must be between 0 and {MAX_LOOKBACK_HOURS} hours")
    if lookback * 60 < interval:
        raise OperatorError("the look-back must cover the interval, or meetings are missed")
    if changes:
        sets = ", ".join(f"{k} = %s" for k in sorted(changes))
        conn.execute(
            f"UPDATE fireflies_settings SET {sets}, updated_at = now(), updated_by = %s"  # noqa: S608
            " WHERE id",
            (*(changes[k] for k in sorted(changes)), actor),
        )
        audit(
            conn,
            actor,
            "fireflies_schedule_change",
            "fireflies_settings",
            None,
            {"fields": sorted(changes)},
        )
    return read_schedule(conn)


def request_run(conn: Connection, actor: str, hours: float | None = None) -> None:
    if hours is not None and not 0 < hours <= MAX_REQUEST_HOURS:
        raise OperatorError(f"hours must be between 0 and {MAX_REQUEST_HOURS} (93 days)")
    conn.execute(
        "UPDATE fireflies_settings SET run_requested_at = now(), run_requested_hours = %s WHERE id",
        (None if hours is None else math.ceil(hours * 100) / 100,),
    )
    audit(conn, actor, "fireflies_capture_requested", "fireflies_settings", None, {"hours": hours})


def capture_in_progress(conn: Connection, now: datetime) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM ops_runs WHERE kind = 'fireflies_capture' AND status = 'running'"
            " AND started_at > %s LIMIT 1",
            (now - RUNNING_GRACE,),
        ).fetchone()
        is not None
    )


def begin_capture(conn: Connection, decision: Decision, now: datetime) -> int:
    """Record the start and clear any "run now" request; the caller commits."""
    conn.execute(
        "UPDATE fireflies_settings SET last_capture_started_at = %s, run_requested_at = NULL,"
        " run_requested_hours = NULL WHERE id",
        (now,),
    )
    row = required(
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, window_hours)"
            " VALUES ('fireflies_capture', %s, %s) RETURNING id",
            (decision.trigger, decision.hours),
        ).fetchone()
    )
    return int(row["id"])
