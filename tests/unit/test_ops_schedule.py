"""When is a scheduled capture due? Pure decisions, no database."""

from datetime import UTC, datetime, timedelta

import pytest

from ci_core.ops import Schedule, capture_decision

# Wednesday 2026-10-28 15:00 UTC = 10:00 in Chicago (CDT).
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def schedule(**changes: object) -> Schedule:
    base: dict[str, object] = {
        "capture_enabled": True,
        "capture_interval_min": 60,
        "capture_window_hours": 2.0,
        "active_start_hour": 0,
        "active_end_hour": 24,
        "weekdays_only": False,
        "time_zone": "America/Chicago",
        "processing_enabled": True,
        "run_requested_at": None,
        "run_requested_hours": None,
        "last_capture_started_at": None,
        "updated_at": NOW,
        "updated_by": "pytest",
    }
    return Schedule(**(base | changes))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "now", "due", "reason"),
    [
        ({}, NOW, True, "due"),
        ({"capture_enabled": False}, NOW, False, "capture is turned off"),
        ({"last_capture_started_at": NOW - timedelta(minutes=30)}, NOW, False, "not due yet"),
        # One minute of slack so 5-minute ticks keep an hourly schedule hourly.
        ({"last_capture_started_at": NOW - timedelta(minutes=59, seconds=30)}, NOW, True, "due"),
        ({"active_start_hour": 11, "active_end_hour": 18}, NOW, False, "outside active hours"),
        ({"active_start_hour": 10, "active_end_hour": 18}, NOW, True, "due"),
        ({"weekdays_only": True}, NOW + timedelta(days=3), False, "weekend"),  # Saturday
        ({"weekdays_only": True}, NOW, True, "due"),
        # A "run now" request wins even when the schedule is off.
        (
            {"capture_enabled": False, "run_requested_at": NOW, "run_requested_hours": 24.0},
            NOW,
            True,
            "run requested",
        ),
    ],
)
def test_capture_decision(
    changes: dict[str, object], now: datetime, due: bool, reason: str
) -> None:
    decision = capture_decision(schedule(**changes), now)
    assert (decision.due, decision.reason) == (due, reason)


def test_requested_run_uses_its_own_window() -> None:
    decision = capture_decision(schedule(run_requested_at=NOW, run_requested_hours=24.0), NOW)
    assert (decision.trigger, decision.hours) == ("manual", 24.0)
    default = capture_decision(schedule(run_requested_at=NOW), NOW)
    assert default.hours == 2.0
    scheduled = capture_decision(schedule(), NOW)
    assert (scheduled.trigger, scheduled.hours) == ("schedule", 2.0)
