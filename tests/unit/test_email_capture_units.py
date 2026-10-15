"""Normalizer, fingerprints and the fixture Graph backend (delta paging, 410, 429)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ci_core.contracts import Direction
from ci_email.fixture_graph import DELTA_PAGE_SIZE, FixtureGraphClient, FixtureMailStore
from ci_email.graph import GraphDeltaExpired, GraphForbidden, GraphThrottled
from ci_email.normalize import content_fingerprint, normalize_message

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "email"
SINCE = datetime(2026, 9, 1, tzinfo=UTC)
PAT = "pat.morgan@finishes.example"


@pytest.fixture
def store(tmp_path: Path) -> FixtureMailStore:
    s = FixtureMailStore(tmp_path / "mail.json")
    s.reset_from_fixtures(FIXTURES)
    return s


def _mailbox_id(store: FixtureMailStore, address: str) -> str:
    return str(next(m["id"] for m in store.read()["mailboxes"].values() if m["address"] == address))


def _inbox(client: FixtureGraphClient, mbx: str) -> str:
    return next(f.id for f in client.list_folders(mbx) if f.well_known_name == "inbox")


def _drain(
    client: FixtureGraphClient, mbx: str, folder: str, link: str | None
) -> tuple[list[str], list[str], str]:
    added: list[str] = []
    removed: list[str] = []
    while True:
        page = client.message_delta(mbx, folder, link, SINCE)
        added += [c.message_id for c in page.changes if not c.removed]
        removed += [c.message_id for c in page.changes if c.removed]
        if page.next_link:
            link = page.next_link
            continue
        assert page.delta_link is not None
        return added, removed, page.delta_link


def test_fixtures_are_synthetic_only() -> None:
    for path in (FIXTURES / "messages").glob("*.json"):
        doc = json.loads(path.read_text())
        m = doc["message"]
        addresses = [m["from"]["emailAddress"]["address"]] + [
            r["emailAddress"]["address"] for k in ("toRecipients", "ccRecipients") for r in m[k]
        ]
        assert all(a.endswith(".example") for a in addresses), path.name


def test_delta_pages_then_reports_only_changes(store: FixtureMailStore) -> None:
    client = FixtureGraphClient(store)
    mbx = _mailbox_id(store, PAT)
    inbox = _inbox(client, mbx)
    added, removed, link = _drain(client, mbx, inbox, None)
    assert len(added) > DELTA_PAGE_SIZE and not removed  # paging exercised
    msg = {
        "subject": "new",
        "from": {"emailAddress": {"address": "a@b.example"}},
        "toRecipients": [],
        "receivedDateTime": "2026-10-15T10:00:00Z",
        "body": {"content": "x"},
    }
    new_id = store.deliver(PAT, "inbox", msg)
    store.move(added[0], "Projects")
    added2, removed2, _ = _drain(client, mbx, inbox, link)
    assert added2 == [new_id] and removed2 == [added[0]]


def test_old_mail_is_outside_the_backfill_window(store: FixtureMailStore) -> None:
    client = FixtureGraphClient(store)
    mbx = _mailbox_id(store, PAT)
    added, _, _ = _drain(client, mbx, _inbox(client, mbx), None)
    subjects = {client.get_message(mbx, i)["subject"] for i in added}
    assert "Q2 budget recap" not in subjects


def test_simulated_410_and_429(store: FixtureMailStore) -> None:
    client = FixtureGraphClient(store)
    mbx = _mailbox_id(store, PAT)
    inbox = _inbox(client, mbx)
    _, _, link = _drain(client, mbx, inbox, None)
    store.simulate(expire_delta_for=PAT)
    with pytest.raises(GraphDeltaExpired):
        client.message_delta(mbx, inbox, link, SINCE)
    store.simulate(throttle_calls=1)
    with pytest.raises(GraphThrottled):
        client.list_mailboxes()
    client.list_mailboxes()  # only the next call


def test_excluded_mailbox_returns_403(store: FixtureMailStore) -> None:
    client = FixtureGraphClient(store)
    with pytest.raises(GraphForbidden):
        client.probe_mailbox(_mailbox_id(store, "exec.private@finishes.example"))


def _copies(store: FixtureMailStore, scenario: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    state = store.read()
    return [
        (state["mailboxes"][e["mailbox_id"]]["address"], e["message"])
        for e in state["messages"].values()
        if e["scenario"] == scenario
    ]


def test_normalizer_direction_unique_body_and_fingerprints(store: FixtureMailStore) -> None:
    ((addr, sent),) = _copies(store, "16_sent_item")
    assert (
        normalize_message(
            sent, connection_ref="t", mailbox_id="m", mailbox_address=addr
        ).communication.direction
        is Direction.OUTBOUND
    )
    ((addr, thread),) = _copies(store, "12_long_thread")
    body = normalize_message(
        thread, connection_ref="t", mailbox_id="m", mailbox_address=addr
    ).communication.body_text
    assert body is not None and "Original Message" not in body  # uniqueBody indexed
    prints = {
        content_fingerprint(
            normalize_message(m, connection_ref="t", mailbox_id="m", mailbox_address=a)
        )
        for a, m in _copies(store, "14_same_message_three_copies")
    }
    assert len(prints) == 1 and None not in prints  # incl. the BCC copy
    for a, m in _copies(store, "01_invoice_approval"):  # attachments unfetched
        assert (
            content_fingerprint(
                normalize_message(m, connection_ref="t", mailbox_id="m", mailbox_address=a)
            )
            is None
        )
