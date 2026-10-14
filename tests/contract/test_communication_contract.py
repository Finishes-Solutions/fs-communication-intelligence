"""Contract tests: synthetic email and non-email fixtures validate against one schema."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ci_core.contracts import Communication, CommunicationKind, Provider

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def test_email_fixture_validates() -> None:
    comm = Communication.model_validate(load("email_invoice.json"))
    assert comm.provider is Provider.EMAIL
    assert comm.kind is CommunicationKind.EMAIL
    assert comm.occurred_at == datetime(2026, 10, 1, 14, 5, tzinfo=UTC)
    assert comm.internet_message_id == "<synthetic-4471@acme-sitework.example>"
    assert [a.is_inline for a in comm.attachments] == [False, True]


def test_non_email_fixture_validates_without_email_fields() -> None:
    comm = Communication.model_validate(load("teams_channel_message.json"))
    assert comm.provider is Provider.TEAMS
    assert comm.title is None
    assert comm.internet_message_id is None
    assert comm.attachments == ()
    # A -05:00 offset is normalized to UTC.
    assert comm.occurred_at == datetime(2026, 10, 1, 14, 30, tzinfo=UTC)


def test_every_fixture_round_trips() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        comm = Communication.model_validate_json(path.read_text())
        assert Communication.model_validate_json(comm.model_dump_json()) == comm


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"occurred_at": "2026-10-01T14:05:00"}, "UTC offset"),
        ({"kind": "meeting"}, "not valid for provider"),
        ({"scope": {"scope_kind": "teams_chat", "native_scope_id": "x"}}, "not valid for provider"),
        (
            {"participants": [{"role": "from", "display_name": "No Address"}]},
            "address or a native_id",
        ),
        ({"unexpected_field": 1}, "Extra inputs"),
        ({"provider_attributes": {"nested": {"a": 1}}}, "provider_attributes"),
        ({"contract_version": "2"}, "contract_version"),
    ],
)
def test_email_contract_rejects(change: dict[str, Any], message: str) -> None:
    data = load("email_invoice.json") | change
    with pytest.raises(ValidationError, match=message):
        Communication.model_validate(data)


def test_internet_message_id_is_email_only() -> None:
    data = load("teams_channel_message.json") | {"internet_message_id": "<x@example>"}
    with pytest.raises(ValidationError, match="only valid for email"):
        Communication.model_validate(data)
