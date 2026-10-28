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


def test_dates_are_plain_iso_days() -> None:
    from datetime import UTC, datetime

    import pytest

    from ci_retrieval.mcp_server import _day

    assert _day(" 2026-10-01 ", "since_date") == datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="since_date must be a date like 2026-10-01"):
        _day("10/01/2026", "since_date")
