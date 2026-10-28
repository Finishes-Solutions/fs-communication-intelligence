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
