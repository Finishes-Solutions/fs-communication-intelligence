"""Operations state: the capture schedule, "run now" requests and run history.

The schedule lives in ``ci.ops_settings`` so the admin console (or ``ci
schedule``) can change it without redeploying. The ingestion app ticks every
five minutes and asks :func:`capture_decision` whether a capture is due.
Run history holds counts and codes only, never content.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ci_core.db import Connection, required
from ci_core.operator import OperatorError, audit

INTERVALS = (15, 30, 60, 120, 240, 720, 1440)
MAX_REQUEST_HOURS = 24 * 93  # a requested backfill; the scheduled window stays <= 744
# A capture older than this is presumed dead (the ingestion app stops a run after 2 hours).
RUNNING_CAPTURE_GRACE = timedelta(minutes=130)
Trigger = Literal["schedule", "manual"]


@dataclass(frozen=True)
class Schedule:
    capture_enabled: bool
    capture_interval_min: int
    capture_window_hours: float
    active_start_hour: int
    active_end_hour: int
    weekdays_only: bool
    time_zone: str
    processing_enabled: bool
    run_requested_at: datetime | None
    run_requested_hours: float | None
    last_capture_started_at: datetime | None
    updated_at: datetime
    updated_by: str
    # Paid per-character checks; off only while testing (migration 0012).
    prompt_shield_enabled: bool = True
    masking_enabled: bool = True
    # Azure OpenAI chat deployment for classification (migration 0014); None keeps
    # the app setting AZURE_OPENAI_CHAT_DEPLOYMENT.
    chat_deployment: str | None = None
    # Reviewed emails shown to the classifier per call (migration 0023); 0 = off.
    review_examples: int = 6

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data


@dataclass(frozen=True)
class Decision:
    due: bool
    reason: str
    trigger: Trigger = "schedule"
    hours: float = 0.0


def capture_decision(schedule: Schedule, now: datetime) -> Decision:
    """Is a capture due now? A pending "run now" request always wins."""
    if schedule.run_requested_at is not None:
        hours = schedule.run_requested_hours or schedule.capture_window_hours
        return Decision(True, "run requested", "manual", hours)
    if not schedule.capture_enabled:
        return Decision(False, "capture is turned off")
    local = now.astimezone(ZoneInfo(schedule.time_zone))
    if schedule.weekdays_only and local.weekday() >= 5:
        return Decision(False, "weekend")
    if not schedule.active_start_hour <= local.hour < schedule.active_end_hour:
        return Decision(False, "outside active hours")
    last = schedule.last_capture_started_at
    # Ticks are 5 minutes apart; allow a minute of drift so an hourly
    # schedule does not slip to every 65 minutes.
    if last is not None and now - last < timedelta(minutes=schedule.capture_interval_min - 1):
        return Decision(False, "not due yet")
    return Decision(True, "due", "schedule", schedule.capture_window_hours)


def read_schedule(conn: Connection) -> Schedule:
    row = required(conn.execute("SELECT * FROM ops_settings WHERE id").fetchone())
    # Only known columns: a database migrated ahead of this code still reads.
    row = {k: v for k, v in row.items() if k in _FIELDS}
    row["capture_window_hours"] = float(row["capture_window_hours"])
    if row["run_requested_hours"] is not None:
        row["run_requested_hours"] = float(row["run_requested_hours"])
    return Schedule(**row)


_FIELDS = {f.name for f in fields(Schedule)}
_EDITABLE = {
    "capture_enabled",
    "capture_interval_min",
    "capture_window_hours",
    "active_start_hour",
    "active_end_hour",
    "weekdays_only",
    "time_zone",
    "processing_enabled",
    "prompt_shield_enabled",
    "masking_enabled",
    "chat_deployment",
    "review_examples",
}
DEPLOYMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def update_schedule(conn: Connection, actor: str, **changes: Any) -> Schedule:
    """Validate and save schedule changes (operator); audited with the changed fields."""
    unknown = set(changes) - _EDITABLE
    if unknown:
        raise OperatorError(f"unknown schedule fields: {', '.join(sorted(unknown))}")
    current = read_schedule(conn)
    if "chat_deployment" in changes:
        name = (changes["chat_deployment"] or "").strip() or None
        if name is not None and not DEPLOYMENT_NAME.match(name):
            raise OperatorError("a deployment name is letters, digits, '.', '_' or '-'")
        changes["chat_deployment"] = name
    proposed = replace(current, **changes)
    if proposed.capture_interval_min not in INTERVALS:
        raise OperatorError(f"interval must be one of {INTERVALS} minutes")
    if not 0 < proposed.capture_window_hours <= 744:
        raise OperatorError("window must be between 0 and 744 hours")
    if proposed.capture_window_hours * 60 < proposed.capture_interval_min:
        raise OperatorError("the window must cover the interval, or mail between runs is missed")
    examples = proposed.review_examples
    if isinstance(examples, bool) or not isinstance(examples, int) or not 0 <= examples <= 12:
        raise OperatorError("review examples must be a whole number from 0 to 12")
    if not 0 <= proposed.active_start_hour < proposed.active_end_hour <= 24:
        raise OperatorError("active hours must satisfy 0 <= start < end <= 24")
    try:
        ZoneInfo(proposed.time_zone)
    except Exception as exc:
        raise OperatorError(f"unknown time zone {proposed.time_zone!r}") from exc
    # Only what actually changes is written and audited (this also lets the console
    # save before migration 0012 adds the switch columns, while they stay on).
    changes = {k: v for k, v in changes.items() if getattr(current, k) != v}
    if not changes:
        return current
    assignments = ", ".join(f"{k} = %({k})s" for k in sorted(changes))
    conn.execute(
        f"UPDATE ops_settings SET {assignments}, updated_at = now(),"  # noqa: S608 - keys whitelisted
        " updated_by = %(actor)s WHERE id",
        {**changes, "actor": actor},
    )
    audit(
        conn,
        actor,
        "schedule_change",
        "ops_settings",
        None,
        {k: (str(v) if not isinstance(v, bool | int) else v) for k, v in changes.items()},
    )
    return read_schedule(conn)


def request_run(conn: Connection, actor: str, hours: float | None = None) -> None:
    """Ask for a capture at the next tick (within about 5 minutes)."""
    if hours is not None and not 0 < hours <= MAX_REQUEST_HOURS:
        raise OperatorError(f"hours must be between 0 and {MAX_REQUEST_HOURS} (93 days)")
    conn.execute(
        "UPDATE ops_settings SET run_requested_at = now(), run_requested_hours = %s WHERE id",
        (hours,),
    )
    audit(conn, actor, "capture_requested", "ops_settings", None, {"hours": hours})


def hours_since(day: str, now: datetime, time_zone: str = "America/Chicago") -> float:
    """Hours from midnight (local time) on ``day`` (YYYY-MM-DD) to ``now``, rounded up."""
    try:
        start = datetime.combine(date.fromisoformat(day), time(0), ZoneInfo(time_zone))
    except ValueError as exc:
        raise OperatorError("give the date as YYYY-MM-DD, e.g. 2026-09-01") from exc
    hours = math.ceil((now - start).total_seconds() / 3600)
    if hours <= 0:
        raise OperatorError("that date is in the future")
    if hours > MAX_REQUEST_HOURS:
        raise OperatorError(f"that is more than {MAX_REQUEST_HOURS // 24} days back")
    return float(hours)


def capture_in_progress(conn: Connection, now: datetime) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM ops_runs WHERE kind = 'capture' AND status = 'running'"
            " AND started_at > %s LIMIT 1",
            (now - RUNNING_CAPTURE_GRACE,),
        ).fetchone()
        is not None
    )


def begin_capture(conn: Connection, decision: Decision, now: datetime) -> int:
    """Record the start and clear any "run now" request; the caller commits."""
    conn.execute(
        "UPDATE ops_settings SET last_capture_started_at = %s, run_requested_at = NULL,"
        " run_requested_hours = NULL WHERE id",
        (now,),
    )
    return start_run(conn, "capture", decision.trigger, decision.hours)


def close_stale_runs(conn: Connection, kind: str, older_than: timedelta, now: datetime) -> int:
    """Mark runs of ``kind`` still 'running' after ``older_than`` as failed (code
    ``stale_run``): their worker was stopped without recording an end (a restart,
    a deploy, the host's time limit), and the history should not show them forever."""
    rows = conn.execute(
        "UPDATE ops_runs SET status = 'failed', finished_at = %s, error_code = 'stale_run'"
        " WHERE kind = %s AND status = 'running' AND started_at < %s RETURNING id",
        (now, kind, now - older_than),
    ).fetchall()
    return len(rows)


def update_run_counts(conn: Connection, run_id: int, counts: dict[str, int]) -> None:
    """Counts so far for a run still in progress (the console shows them live)."""
    conn.execute(
        "UPDATE ops_runs SET counts = %s WHERE id = %s AND status = 'running'",
        (json.dumps(counts), run_id),
    )


def start_run(conn: Connection, kind: str, trigger: Trigger, hours: float | None = None) -> int:
    row = required(
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, window_hours) VALUES (%s, %s, %s) RETURNING id",
            (kind, trigger, hours),
        ).fetchone()
    )
    return int(row["id"])


def finish_run(
    conn: Connection,
    run_id: int,
    status: str,
    counts: dict[str, int],
    error_code: str | None = None,
) -> None:
    conn.execute(
        "UPDATE ops_runs SET finished_at = now(), status = %s, counts = %s, error_code = %s"
        " WHERE id = %s",
        (status, json.dumps(counts), error_code, run_id),
    )


def recent_runs(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, kind, trigger, window_hours, started_at, finished_at, status, counts,"
        " error_code FROM ops_runs ORDER BY started_at DESC LIMIT %s",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [_jsonable(r) for r in rows]


def overview(conn: Connection) -> dict[str, Any]:
    """Counts only, for the dashboard (operator role)."""

    def rows(sql: str) -> list[dict[str, Any]]:
        return [_jsonable(r) for r in conn.execute(sql).fetchall()]

    return {
        "mailboxes": rows("SELECT * FROM ops_mailbox_states ORDER BY mailboxes DESC"),
        "versions": rows("SELECT * FROM ops_version_states ORDER BY versions DESC"),
        "backlog": rows("SELECT * FROM ops_backlog")[0],
        "failures": rows("SELECT * FROM ops_failures ORDER BY versions DESC"),
        "labels": rows(
            "SELECT * FROM ops_label_counts WHERE decision = 'yes' ORDER BY messages DESC"
        ),
        "uncertain_labels": rows(
            "SELECT * FROM ops_label_counts"
            " WHERE decision IN ('review', 'unresolved')"
            " ORDER BY messages DESC"
        ),
        "extraction": rows("SELECT * FROM ops_extraction_counts ORDER BY attachments DESC"),
        "originals": rows("SELECT * FROM ops_original_counts ORDER BY attachments DESC"),
    }


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif hasattr(value, "is_finite"):  # Decimal
            out[key] = float(value)
        else:
            out[key] = value
    return out
