"""The Microsoft Graph surface the email pipeline uses, as an interface.

Two implementations: the real client (PR 5, not built yet) and
``FixtureGraphClient`` (``fixture_graph.py``), which serves synthetic
mailboxes from a local store. Both raise the same errors, so capture code
handles 403, 404, 410 and 429 identically.

Real calls must send ``Prefer: IdType="ImmutableId"`` everywhere and
``Prefer: outlook.body-content-type="text"`` when reading messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

# Graph $select for a message fetch (CLAUDE.md section 6.3), plus attachment
# metadata via $expand=attachments($select=id,name,contentType,size,isInline).
MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,changeKey,subject,from,sender,toRecipients,"
    "ccRecipients,bccRecipients,receivedDateTime,sentDateTime,body,uniqueBody,"
    "hasAttachments,webLink,isDraft,parentFolderId"
)

# Well-known folders capture never reads. Sent Items is skipped only when
# EMAIL_INCLUDE_SENT is false (open decision D-S1).
SKIPPED_FOLDERS = frozenset(
    {"deleteditems", "junkemail", "drafts", "outbox", "conversationhistory", "syncissues"}
)


class GraphError(Exception):
    """Base class. ``code`` is a content-free reason code for jobs and logs."""

    code = "graph_error"


class GraphForbidden(GraphError):
    """403: outside the Exchange RBAC scope. The only valid negative test."""

    code = "graph_403"


class GraphNotFound(GraphError):
    code = "graph_404"


class GraphDeltaExpired(GraphError):
    """410 on a delta link: drop the checkpoint and resync from the boundary."""

    code = "graph_410_delta_expired"


class GraphThrottled(GraphError):
    code = "graph_429"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"throttled; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class MailboxInfo:
    id: str  # Entra object id
    address: str  # primary SMTP address, lowercase
    display_name: str
    recipient_type: str  # UserMailbox | SharedMailbox | ...
    account_enabled: bool  # shared mailboxes are False: never filter on it


@dataclass(frozen=True)
class FolderInfo:
    id: str
    display_name: str
    well_known_name: str | None


@dataclass(frozen=True)
class DeltaChange:
    message_id: str
    removed: bool


@dataclass(frozen=True)
class DeltaPage:
    changes: tuple[DeltaChange, ...]
    next_link: str | None  # more pages in this round
    delta_link: str | None  # set on the last page; persist only after all pages commit


class GraphClient(Protocol):
    def list_mailboxes(self) -> list[MailboxInfo]: ...

    def probe_mailbox(self, mailbox_id: str) -> None:
        """Raise GraphForbidden (403) or GraphNotFound (no mailbox); return if readable."""

    def list_folders(self, mailbox_id: str) -> list[FolderInfo]: ...

    def message_delta(
        self, mailbox_id: str, folder_id: str, link: str | None, received_since: datetime
    ) -> DeltaPage:
        """First call with link=None starts a sync from ``received_since``."""

    def get_message(self, mailbox_id: str, message_id: str) -> dict[str, Any]: ...
