"""MCP results link to the admin console's read-only email page (the Outlook
web_link opens only in the mailbox owner's own Outlook)."""

from ci_retrieval.mcp_server import with_links

BASE = "https://admin.example/"


def test_every_email_gets_a_view_url_and_stored_files_a_download_url() -> None:
    data = {
        "actions": [{"communication_id": "c1", "subject": "s"}],
        "communication_id": "c2",
        "attachments": [{"file_id": "f" * 64, "name": "a.pdf"}, {"file_id": None, "name": "b"}],
    }
    linked = with_links(data, BASE)
    assert linked["actions"][0]["view_url"] == "https://admin.example/read/c1"
    assert linked["view_url"] == "https://admin.example/read/c2"
    stored, missing = linked["attachments"]
    assert stored["download_url"] == f"https://admin.example/read/c2?file={'f' * 64}"
    assert "download_url" not in missing and "view_url" not in stored
    assert data["actions"][0] == {"communication_id": "c1", "subject": "s"}  # input untouched


def test_without_a_console_address_results_are_unchanged() -> None:
    data = {"communication_id": "c1"}
    assert with_links(data, None) is data


def test_dates_are_iso_days_in_central_time() -> None:
    from datetime import UTC, datetime

    import pytest

    from ci_retrieval.mcp_server import _day

    # Midnight in Waller (CDT, UTC-5 in October), not midnight UTC.
    assert _day(" 2026-10-01 ", "since_date") == datetime(2026, 10, 1, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="since_date must be a date like 2026-10-01"):
        _day("10/01/2026", "since_date")
    with pytest.raises(ValueError, match="since_date must be a date like 2026-10-01"):
        _day("2026-10-01T09:00", "since_date")  # the older argument stays date-only


def test_times_and_ranges() -> None:
    from datetime import UTC, datetime, timedelta

    import pytest

    from ci_retrieval.mcp_server import _moment, _window

    def utc(*parts: int) -> datetime:
        return datetime(*parts, tzinfo=UTC)  # type: ignore[misc, arg-type]

    # A time without an offset is Central; with Z or an offset it is as written.
    assert _moment("2026-11-18T14:30", "since") == utc(2026, 11, 18, 20, 30)
    assert _moment("2026-11-18 14:30:15", "since") == utc(2026, 11, 18, 20, 30, 15)
    assert _moment("2026-11-18T14:30Z", "since") == utc(2026, 11, 18, 14, 30)
    assert _moment("2026-11-18T14:30-05:00", "since") == utc(2026, 11, 18, 19, 30)
    # An end date includes that whole day; across the daylight-saving change too
    # (Central time fell back on 2026-11-01, so that day had 25 hours).
    assert _moment("2026-11-18", "until", end=True) == utc(2026, 11, 19, 6)
    day_end = _moment("2026-11-01", "until", end=True).astimezone(UTC)
    assert day_end - _moment("2026-11-01", "since").astimezone(UTC) == timedelta(hours=25)
    # An end time is exclusive and is not moved to the end of the day.
    assert _moment("2026-11-18T12:00", "until", end=True) == utc(2026, 11, 18, 18)
    with pytest.raises(ValueError, match="since must be a date like 2026-10-01 or a date and"):
        _moment("yesterday", "since")

    one_day = _window(None, since="2026-11-18", until="2026-11-18")
    assert one_day == {"since": utc(2026, 11, 18, 6), "until": utc(2026, 11, 19, 6)}
    # since/until come before since_date/until_date, which come before since_days.
    assert _window(7, "2026-01-01", "2026-12-31", "2026-11-18T08:00", None) == {
        "since": utc(2026, 11, 18, 14),
        "until": utc(2027, 1, 1, 6),
    }
    assert _window(None, "2026-01-01")["since"] == utc(2026, 1, 1, 6)
    recent = _window(0.25)["since"]
    assert recent is not None
    assert (
        timedelta(hours=5, minutes=59) < datetime.now(UTC) - recent < timedelta(hours=6, minutes=1)
    )
    assert _window(None) == {"since": None, "until": None}
    with pytest.raises(ValueError, match="since must be before until"):
        _window(None, since="2026-11-18T12:00", until="2026-11-18T08:00")
