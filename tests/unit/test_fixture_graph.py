"""The fixture Graph client behaves like the Graph surface capture depends on."""

from datetime import UTC, datetime

import pytest

from ci_email.fixture import Fault, FixtureGraphClient, load_tenant
from ci_email.graph import (
    DeltaItem,
    DeltaTokenExpired,
    ItemNotFound,
    MailboxAccessDenied,
    MailboxNotFound,
    Throttled,
    capture_folders,
)

PAT = "00000000-0000-0000-0000-0000000000aa"
INBOX = "fld-pat-inbox"
PROJECTS = "fld-pat-projects"
BACKFILL = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def graph() -> FixtureGraphClient:
    return FixtureGraphClient.from_directory(page_size=2)


def drain(
    graph: FixtureGraphClient, folder: str, link: str | None = None, since: datetime | None = None
) -> tuple[list[DeltaItem], str]:
    items: list[DeltaItem] = []
    page = graph.delta(PAT, folder, link=link, received_since=since)
    items.extend(page.items)
    while page.next_link:
        page = graph.delta(PAT, folder, link=page.next_link)
        items.extend(page.items)
    assert page.delta_link is not None
    return items, page.delta_link


def test_fixture_set_is_consistent_and_synthetic() -> None:
    tenant, messages = load_tenant()
    assert len(messages) >= 20
    for m in messages:
        for field in ("from", "toRecipients"):
            value = m.message[field]
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                assert entry["emailAddress"]["address"].endswith(".example"), m.name
    assert {mb.address.split("@")[1] for mb in tenant.mailboxes} == {"finishessolutions.example"}


def test_inventory_lists_every_mailbox_and_access_errors(graph: FixtureGraphClient) -> None:
    mailboxes = {m.address: m for m in graph.list_mailboxes()}
    assert len(mailboxes) == 6
    assert mailboxes["ap@finishessolutions.example"].purpose == "shared"
    with pytest.raises(MailboxAccessDenied):
        graph.list_folders(mailboxes["riley.outside@finishessolutions.example"].id)
    with pytest.raises(MailboxNotFound):
        graph.list_folders(mailboxes["no.mailbox@finishessolutions.example"].id)


@pytest.mark.parametrize(("include_sent", "has_sent"), [(True, True), (False, False)])
def test_capture_folders_skip_well_known(
    graph: FixtureGraphClient, include_sent: bool, has_sent: bool
) -> None:
    names = {
        f.well_known_name or f.display_name
        for f in capture_folders(graph.list_folders(PAT), include_sent=include_sent)
    }
    assert {"inbox", "archive", "Projects"} <= names
    assert names.isdisjoint(
        {"drafts", "junkemail", "deleteditems", "outbox", "conversationhistory", "syncissues"}
    )
    assert ("sentitems" in names) is has_sent


def test_initial_delta_pages_and_respects_backfill(graph: FixtureGraphClient) -> None:
    all_items, _ = drain(graph, INBOX)
    bounded, delta_link = drain(graph, INBOX, since=BACKFILL)
    old = next(m for m in graph.messages if m.name == "22_before_backfill")
    assert old.id in {i.id for i in all_items}
    assert old.id not in {i.id for i in bounded}
    assert len(bounded) > 2  # more than one page at page_size=2
    assert not any(i.removed for i in bounded)
    again, _ = drain(graph, INBOX, link=delta_link)
    assert again == []  # nothing changed since the delta link


def test_move_and_delete_show_as_removed_then_added(graph: FixtureGraphClient) -> None:
    _, inbox_link = drain(graph, INBOX, since=BACKFILL)
    _, projects_link = drain(graph, PROJECTS, since=BACKFILL)
    moved = next(m for m in graph.messages if m.name == "15_moved_later")
    deleted = next(m for m in graph.messages if m.name == "16_deleted_later")
    graph.advance()
    inbox_changes, _ = drain(graph, INBOX, link=inbox_link)
    assert set(inbox_changes) == {DeltaItem(moved.id, True), DeltaItem(deleted.id, True)}
    projects_changes, _ = drain(graph, PROJECTS, link=projects_link)
    assert projects_changes == [DeltaItem(moved.id, False)]
    # Immutable ids survive the move; the parent folder changes.
    assert graph.get_message(PAT, moved.id)["parentFolderId"] == PROJECTS
    with pytest.raises(ItemNotFound):
        graph.get_message(PAT, deleted.id)


def test_expired_delta_token_and_throttling(graph: FixtureGraphClient) -> None:
    _, link = drain(graph, INBOX, since=BACKFILL)
    graph.inject(Fault("expired", folder_id=INBOX))
    with pytest.raises(DeltaTokenExpired):
        graph.delta(PAT, INBOX, link=link)
    graph.inject(Fault("throttle", retry_after=7, times=1))
    with pytest.raises(Throttled) as excinfo:
        graph.delta(PAT, INBOX)
    assert excinfo.value.retry_after == 7
    assert graph.delta(PAT, INBOX).items  # one-shot fault is spent


def test_throttle_does_not_consume_a_page(graph: FixtureGraphClient) -> None:
    first = graph.delta(PAT, INBOX, received_since=BACKFILL)
    assert first.next_link
    graph.inject(Fault("throttle"))
    with pytest.raises(Throttled):
        graph.delta(PAT, INBOX, link=first.next_link)
    second = graph.delta(PAT, INBOX, link=first.next_link)
    assert second.items and set(second.items).isdisjoint(first.items)


def test_same_message_in_three_mailboxes_has_distinct_ids() -> None:
    _, messages = load_tenant()
    copies = [m for m in messages if m.name.startswith("14")]
    assert len(copies) == 3
    assert len({m.message["internetMessageId"] for m in copies}) == 1
    assert len({(m.fixture.mailbox, m.id) for m in copies}) == 3
    bcc_copy = next(m for m in copies if m.fixture.mailbox.startswith("ap@"))
    visible = {r["emailAddress"]["address"] for r in bcc_copy.message["toRecipients"]}
    assert bcc_copy.fixture.mailbox not in visible


def test_attachment_metadata_only(graph: FixtureGraphClient) -> None:
    invoice = next(m for m in graph.messages if m.name == "01_invoice_action_required")
    attachments = graph.list_attachments(PAT, invoice.id)
    assert [a["isInline"] for a in attachments] == [False, True]
    assert all("contentBytes" not in a for a in attachments)
