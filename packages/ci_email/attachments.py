"""Attachment handling for increment E1 under ATTACHMENT_UNSUPPORTED_POLICY (ADR-016, D-E1).

In E1 nothing is extracted, so every attachment is unsupported:

- ``hold_message``: any attachment holds the whole message.
- ``publish_partial``: publish the body; report attachments as partial coverage.
- ``exempt_cid_inline``: ignore inline images with a contentId; any other attachment holds.
"""

from __future__ import annotations

from dataclasses import dataclass

from ci_core.contracts import AttachmentMetadata
from ci_core.settings import AttachmentPolicy


@dataclass(frozen=True)
class AttachmentVerdict:
    statuses: dict[str, str]  # native attachment id -> email_attachments.status
    hold_reason: str | None
    partial: bool

    @property
    def decision(self) -> str:
        return "hold" if self.hold_reason else "allow"


def evaluate(
    attachments: tuple[AttachmentMetadata, ...], policy: AttachmentPolicy
) -> AttachmentVerdict:
    statuses: dict[str, str] = {}
    hold = False
    for a in attachments:
        if policy is AttachmentPolicy.PUBLISH_PARTIAL:
            statuses[a.native_id] = "unsupported_partial"
        elif policy is AttachmentPolicy.EXEMPT_CID_INLINE and a.is_inline and a.content_id:
            statuses[a.native_id] = "exempt_inline"
        else:
            statuses[a.native_id] = "unsupported_held"
            hold = True
    partial = any(s == "unsupported_partial" for s in statuses.values())
    return AttachmentVerdict(statuses, "unsupported_attachment" if hold else None, partial)
