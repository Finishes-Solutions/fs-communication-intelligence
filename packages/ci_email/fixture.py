"""Fixture Graph client: serves synthetic mail from ``tests/fixtures/email``.

Layout:

- ``tenant.json``: mailboxes (id, address, purpose, folders) and how each
  responds (``access``: ``ok``, ``forbidden`` = 403, ``no_mailbox`` = 404);
- ``messages/*.json``: one Graph-shaped message each, with a ``fixture`` block
  naming its mailbox and folder, later changes (``after``: move or delete in a
  later round) and the outcome the pipeline should reach (``expect``).

The client starts at round 1. ``advance()`` applies the next round's moves and
deletes, which later delta rounds report. Tests can inject 429 and 410 faults.
Only synthetic data belongs in these fixtures.
"""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field

from ci_email.graph import (
    DeltaItem,
    DeltaPage,
    DeltaTokenExpired,
    ItemNotFound,
    MailboxAccessDenied,
    MailboxNotFound,
    MailboxPurpose,
    MailFolder,
    Throttled,
)
from ci_email.graph import Mailbox as GraphMailbox

WELL_KNOWN_DISPLAY = {
    "inbox": "Inbox",
    "sentitems": "Sent Items",
    "drafts": "Drafts",
    "deleteditems": "Deleted Items",
    "junkemail": "Junk Email",
    "outbox": "Outbox",
    "conversationhistory": "Conversation History",
    "syncissues": "Sync Issues",
    "archive": "Archive",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FixtureMailbox(_Model):
    id: str
    address: str
    display_name: str
    purpose: MailboxPurpose
    access: Literal["ok", "forbidden", "no_mailbox"]
    seed: Literal["approve", "exclude", "none"]
    folders: tuple[str, ...]

    def folder_id(self, folder: str) -> str:
        return f"fld-{self.address.split('@')[0].split('.')[0]}-{folder}"


class FixtureTenant(_Model):
    tenant_id: str
    backfill_since: datetime
    mailboxes: tuple[FixtureMailbox, ...]


class FixtureChange(_Model):
    round: int = Field(ge=2)
    action: Literal["move", "delete"]
    folder: str | None = None


class FixtureExpect(_Model):
    captured: bool
    screening: tuple[str, ...] = ()
    labels_yes: tuple[str, ...] = ()
    disposition: Literal["index", "review", "not_indexed", "restricted", "held", "skipped"]


class FixtureMeta(_Model):
    mailbox: str
    folder: str
    after: tuple[FixtureChange, ...] = ()
    expect: FixtureExpect


class FixtureMessage(_Model):
    name: str
    fixture: FixtureMeta
    message: dict[str, Any]
    attachments: tuple[dict[str, Any], ...] = ()

    @property
    def id(self) -> str:
        return str(self.message["id"])


class FixtureError(ValueError):
    pass


def default_fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "email"


def load_tenant(directory: Path | None = None) -> tuple[FixtureTenant, list[FixtureMessage]]:
    """Load and cross-check the fixture set; raises ``FixtureError`` on inconsistency."""
    directory = directory or default_fixtures_dir()
    tenant = FixtureTenant.model_validate_json((directory / "tenant.json").read_text())
    by_address = {m.address: m for m in tenant.mailboxes}
    messages: list[FixtureMessage] = []
    for path in sorted((directory / "messages").glob("*.json")):
        data = json.loads(path.read_text())
        fm = FixtureMessage.model_validate({"name": path.stem, **data})
        mailbox = by_address.get(fm.fixture.mailbox)
        if mailbox is None:
            raise FixtureError(f"{path.name}: unknown mailbox")
        for folder in (fm.fixture.folder, *(c.folder for c in fm.fixture.after if c.folder)):
            if folder not in mailbox.folders:
                raise FixtureError(f"{path.name}: folder {folder} not in mailbox")
        if fm.message.get("parentFolderId") != mailbox.folder_id(fm.fixture.folder):
            raise FixtureError(f"{path.name}: parentFolderId does not match fixture folder")
        messages.append(fm)
    ids = [(m.fixture.mailbox, m.id) for m in messages]
    if len(set(ids)) != len(ids):
        raise FixtureError("duplicate message id within a mailbox")
    return tenant, messages


@dataclass
class Fault:
    """An injected failure. ``times`` counts down; matching is on the given fields."""

    kind: Literal["throttle", "expired"]
    operation: Literal["delta", "get_message"] = "delta"
    mailbox_id: str | None = None
    folder_id: str | None = None
    retry_after: float = 1.0
    times: int = 1


@dataclass
class _Located:
    mailbox_id: str
    folder: str
    fixture: FixtureMessage


@dataclass
class _Round:
    items: list[DeltaItem]
    mailbox_id: str
    folder_id: str
    final_seq: int


@dataclass
class FixtureGraphClient:
    tenant: FixtureTenant
    messages: list[FixtureMessage]
    page_size: int = 2
    round: int = 1
    calls: dict[str, int] = field(default_factory=dict)
    _located: dict[tuple[str, str], _Located] = field(default_factory=dict)
    _log: list[tuple[int, str, str, str, bool]] = field(default_factory=list)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _rounds: dict[str, _Round] = field(default_factory=dict)
    _faults: list[Fault] = field(default_factory=list)

    @classmethod
    def from_directory(
        cls, directory: Path | None = None, page_size: int = 2
    ) -> FixtureGraphClient:
        tenant, messages = load_tenant(directory)
        return cls(tenant, messages, page_size=page_size)

    def __post_init__(self) -> None:
        by_address = {m.address: m for m in self.tenant.mailboxes}
        for fm in self.messages:
            mailbox = by_address[fm.fixture.mailbox]
            self._located[(mailbox.id, fm.id)] = _Located(mailbox.id, fm.fixture.folder, fm)
            self._record(mailbox.id, fm.fixture.folder, fm.id, removed=False)

    # -- test controls ----------------------------------------------------

    def advance(self) -> None:
        """Apply the next round's scheduled moves and deletes."""
        self.round += 1
        for key, loc in list(self._located.items()):
            for change in loc.fixture.fixture.after:
                if change.round != self.round:
                    continue
                self._record(loc.mailbox_id, loc.folder, loc.fixture.id, removed=True)
                if change.action == "delete":
                    del self._located[key]
                elif change.folder is None:
                    raise FixtureError(f"{loc.fixture.name}: a move needs a folder")
                else:
                    loc.folder = change.folder
                    self._record(loc.mailbox_id, loc.folder, loc.fixture.id, removed=False)

    def inject(self, fault: Fault) -> None:
        self._faults.append(fault)

    # -- GraphClient --------------------------------------------------------

    def list_mailboxes(self) -> list[GraphMailbox]:
        self._count("list_mailboxes")
        return [
            GraphMailbox(m.id, m.address.lower(), m.display_name, m.purpose)
            for m in self.tenant.mailboxes
        ]

    def list_folders(self, mailbox_id: str) -> list[MailFolder]:
        self._count("list_folders")
        mailbox = self._mailbox(mailbox_id)
        return [
            MailFolder(
                mailbox.folder_id(name),
                WELL_KNOWN_DISPLAY.get(name, name.title()),
                name if name in WELL_KNOWN_DISPLAY else None,
            )
            for name in mailbox.folders
        ]

    def delta(
        self,
        mailbox_id: str,
        folder_id: str,
        *,
        link: str | None = None,
        received_since: datetime | None = None,
    ) -> DeltaPage:
        self._count("delta")
        mailbox = self._mailbox(mailbox_id)
        folder = self._folder_name(mailbox, folder_id)
        url = urlparse(link) if link else None
        if url is not None and url.path == "/next":
            self._raise_fault("delta", mailbox_id, folder_id, delta_link=False)
            token = parse_qs(url.query)["token"][0]
            return self._page(token)
        if url is not None:
            self._raise_fault("delta", mailbox_id, folder_id, delta_link=True)
            since = int(parse_qs(url.query)["seq"][0])
            latest: dict[str, bool] = {}
            for seq, mbx, fld, mid, removed in self._log:
                if seq > since and mbx == mailbox_id and fld == folder:
                    latest[mid] = removed
            items = [DeltaItem(mid, removed) for mid, removed in latest.items()]
        else:
            self._raise_fault("delta", mailbox_id, folder_id, delta_link=False)
            items = [
                DeltaItem(loc.fixture.id)
                for loc in self._located.values()
                if loc.mailbox_id == mailbox_id
                and loc.folder == folder
                and (
                    received_since is None
                    or datetime.fromisoformat(loc.fixture.message["receivedDateTime"])
                    >= received_since
                )
            ]
        token = f"r{len(self._rounds) + 1}"
        current = self._log[-1][0] if self._log else 0
        self._rounds[token] = _Round(items, mailbox_id, folder_id, current)
        return self._page(token)

    def get_message(self, mailbox_id: str, message_id: str) -> dict[str, Any]:
        self._count("get_message")
        mailbox = self._mailbox(mailbox_id)
        self._raise_fault("get_message", mailbox_id, None, delta_link=False)
        loc = self._located.get((mailbox_id, message_id))
        if loc is None:
            raise ItemNotFound(f"message {message_id} not found")
        message = copy.deepcopy(loc.fixture.message)
        message["parentFolderId"] = mailbox.folder_id(loc.folder)
        return message

    def list_attachments(self, mailbox_id: str, message_id: str) -> list[dict[str, Any]]:
        self._count("list_attachments")
        self._mailbox(mailbox_id)
        loc = self._located.get((mailbox_id, message_id))
        if loc is None:
            raise ItemNotFound(f"message {message_id} not found")
        return [copy.deepcopy(a) for a in loc.fixture.attachments]

    # -- internals ----------------------------------------------------------

    def _count(self, operation: str) -> None:
        self.calls[operation] = self.calls.get(operation, 0) + 1

    def _record(self, mailbox_id: str, folder: str, message_id: str, *, removed: bool) -> None:
        self._log.append((next(self._seq), mailbox_id, folder, message_id, removed))

    def _mailbox(self, mailbox_id: str) -> FixtureMailbox:
        for m in self.tenant.mailboxes:
            if m.id == mailbox_id:
                if m.access == "forbidden":
                    raise MailboxAccessDenied(f"403 for mailbox {mailbox_id}")
                if m.access == "no_mailbox":
                    raise MailboxNotFound(f"404 for mailbox {mailbox_id}")
                return m
        raise MailboxNotFound(f"404 for mailbox {mailbox_id}")

    @staticmethod
    def _folder_name(mailbox: FixtureMailbox, folder_id: str) -> str:
        for name in mailbox.folders:
            if mailbox.folder_id(name) == folder_id:
                return name
        raise ItemNotFound(f"folder {folder_id} not found")

    def _page(self, token: str) -> DeltaPage:
        state = self._rounds.get(token)
        if state is None:
            raise DeltaTokenExpired(f"unknown page token {token}")
        page, rest = state.items[: self.page_size], state.items[self.page_size :]
        state.items = rest
        if rest:
            return DeltaPage(tuple(page), f"fixture://delta/next?token={token}", None)
        del self._rounds[token]
        delta_link = (
            f"fixture://delta/link?mailbox={state.mailbox_id}"
            f"&folder={state.folder_id}&seq={state.final_seq}"
        )
        return DeltaPage(tuple(page), None, delta_link)

    def _raise_fault(
        self, operation: str, mailbox_id: str, folder_id: str | None, *, delta_link: bool
    ) -> None:
        for fault in self._faults:
            if (
                fault.times <= 0
                or fault.operation != operation
                or (fault.mailbox_id and fault.mailbox_id != mailbox_id)
                or (fault.folder_id and fault.folder_id != folder_id)
                or (fault.kind == "expired" and not delta_link)
            ):
                continue
            fault.times -= 1
            if fault.kind == "throttle":
                raise Throttled(fault.retry_after)
            raise DeltaTokenExpired(f"410 for folder {folder_id}")
