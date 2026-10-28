"""The Microsoft Graph surface the email pipeline uses, as an interface.

Two implementations share it: ``FixtureGraphClient`` (``GRAPH_BACKEND=fixture``,
synthetic data, no network) and the real httpx client (PR 5). Capture code
depends only on this module, so it runs unchanged against either.

Graph behavior modelled here, to be re-checked against current Microsoft docs
when the real client is written (they could not be fetched when this was
written):

- every call sends ``Prefer: IdType="ImmutableId"``, so a message keeps its id
  when it moves between folders of the same mailbox;
- per-folder message delta returns pages linked by ``@odata.nextLink`` and ends
  with an ``@odata.deltaLink``; items that left the folder (moved or deleted)
  come back as ``{"id": ..., "@removed": {...}}``;
- the initial delta round accepts ``$filter=receivedDateTime ge <boundary>``;
- an expired delta token answers HTTP 410 and the folder must resync;
- throttling answers HTTP 429 with ``Retry-After`` seconds;
- a mailbox outside the Exchange RBAC scope answers 403; a user without a
  mailbox answers 404.

Return values carry content (subjects, bodies), so never log them. Log ids,
counts and reason codes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

# "unknown": the real client lists directory users and cannot tell user from
# shared mailboxes without an extra mailbox-settings permission.
MailboxPurpose = Literal["user", "shared", "room", "equipment", "other", "unknown"]

# Well-known folders capture never reads (CLAUDE.md section 6.2). Sent Items
# is skipped only when EMAIL_INCLUDE_SENT is false.
SKIPPED_WELL_KNOWN_FOLDERS = frozenset(
    {"deleteditems", "junkemail", "drafts", "outbox", "conversationhistory", "syncissues"}
)
SENT_ITEMS = "sentitems"
WELL_KNOWN_FOLDERS = (*sorted(SKIPPED_WELL_KNOWN_FOLDERS), SENT_ITEMS)

# Message fields capture reads (CLAUDE.md section 6.3).
MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,changeKey,subject,from,sender,toRecipients,"
    "ccRecipients,bccRecipients,receivedDateTime,sentDateTime,body,uniqueBody,"
    "hasAttachments,webLink,isDraft,parentFolderId"
)


@dataclass(frozen=True)
class Mailbox:
    id: str  # Entra object id; source_scopes.native_scope_id
    address: str  # primary SMTP address, lowercase
    display_name: str
    purpose: MailboxPurpose


@dataclass(frozen=True)
class MailFolder:
    id: str
    display_name: str
    well_known_name: str | None


@dataclass(frozen=True)
class DeltaItem:
    id: str
    removed: bool = False


@dataclass(frozen=True)
class DeltaPage:
    items: tuple[DeltaItem, ...]
    next_link: str | None
    delta_link: str | None  # set only on the last page of a round


class GraphError(Exception):
    """Base class. Messages carry ids and status codes only, never content."""


class MailboxAccessDenied(GraphError):
    """HTTP 403: the mailbox is outside the Exchange RBAC scope."""


class MailboxNotFound(GraphError):
    """HTTP 404 on a mailbox: the user has no Exchange mailbox."""


class ItemNotFound(GraphError):
    """HTTP 404 on a message: deleted, or moved out of reach."""


class DeltaTokenExpired(GraphError):
    """HTTP 410: drop the folder checkpoint and resync from the backfill boundary."""


class Throttled(GraphError):
    """HTTP 429: retry after ``retry_after`` seconds."""

    def __init__(self, retry_after: float, message: str = "throttled") -> None:
        super().__init__(f"{message}; retry after {retry_after}s")
        self.retry_after = retry_after


class GraphClient(Protocol):
    def list_mailboxes(self) -> list[Mailbox]: ...

    def list_folders(self, mailbox_id: str) -> list[MailFolder]: ...

    def delta(
        self,
        mailbox_id: str,
        folder_id: str,
        *,
        link: str | None = None,
        received_since: datetime | None = None,
    ) -> DeltaPage:
        """Start a round with ``link=None`` or continue from a next/delta link."""
        ...

    def list_messages(self, mailbox_id: str, *, received_since: datetime) -> list[dict[str, Any]]:
        """Messages received since a time, in every folder, as Graph JSON (MESSAGE_SELECT).

        Used for bounded windows (``ci capture --hours``). Continuous capture
        uses per-folder delta.
        """
        ...

    def well_known_folder_ids(self, mailbox_id: str) -> dict[str, str]:
        """Ids of the WELL_KNOWN_FOLDERS that exist in this mailbox."""
        ...

    def get_message(self, mailbox_id: str, message_id: str) -> dict[str, Any]:
        """The message as Graph JSON for the fields in CLAUDE.md section 6.3."""
        ...

    def list_attachments(self, mailbox_id: str, message_id: str) -> list[dict[str, Any]]:
        """Attachment metadata (no ``contentBytes``), including ``@odata.type``."""
        ...

    def get_attachment_bytes(self, mailbox_id: str, message_id: str, attachment_id: str) -> bytes:
        """Raw bytes of a file attachment."""
        ...


def capture_folders(folders: list[MailFolder], *, include_sent: bool) -> list[MailFolder]:
    """Folders capture reads: everything except the skipped well-known folders."""
    skipped = SKIPPED_WELL_KNOWN_FOLDERS | (frozenset() if include_sent else {SENT_ITEMS})
    return [f for f in folders if f.well_known_name not in skipped]
