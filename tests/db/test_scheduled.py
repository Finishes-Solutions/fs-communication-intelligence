"""The ingestion Function App jobs: switches, empty-scope refusal, then a full run."""

import pytest

from ci_cli.pipeline import include_all_enrolled, tenant_init
from ci_cli.scheduled import run_capture, run_processing
from ci_core.db import connect
from ci_core.operator import exclude_mailbox
from ci_core.settings import load_settings


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("CAPTURE_HOURS", "744")
    monkeypatch.setenv("ATTACHMENT_EXTRACTION_ENABLED", "true")


def test_jobs_are_off_until_switched_on() -> None:
    settings = load_settings()
    assert run_capture(settings) is None
    assert run_processing(settings) is None


def test_first_run_inventories_then_refuses_until_mailboxes_are_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    monkeypatch.setenv("PROCESSING_ENABLED", "true")
    with connect() as conn:
        tenant_init(conn, "pytest")
    assert run_capture(load_settings()) is None  # inventory ran; nothing approved yet
    with connect() as conn:
        include_all_enrolled(conn, "pytest")
        exclude_mailbox(
            conn, "casey.private@finishessolutions.example", "pytest", reason="personal_mailbox"
        )
    captured = run_capture(load_settings())
    assert captured is not None and captured["new_version"] == 20
    processed = run_processing(load_settings())
    assert processed is not None and sum(processed.values()) == 20
    again = run_capture(load_settings())
    assert again is not None and again.get("new_version", 0) == 0
