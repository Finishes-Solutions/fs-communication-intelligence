"""Graph message JSON -> source-neutral ``Communication`` plus email-only metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ci_core.contracts import (
    AttachmentMetadata,
    Communication,
    CommunicationKind,
    Direction,
    Participant,
    ParticipantRole,
    Provider,
    ScopeKind,
    SourceScopeRef,
)

NORMALIZER_VERSION = "email-normalizer.v1"
FINGERPRINT_VERSION = "fp.v1"

_ROLES = (
    ("from", ParticipantRole.FROM),
    ("sender", ParticipantRole.SENDER),
    ("toRecipients", ParticipantRole.TO),
    ("ccRecipients", ParticipantRole.CC),
    ("bccRecipients", ParticipantRole.BCC),
)


@dataclass(frozen=True)
class EmailMessage:
    """One mailbox copy, normalized. Holds content: never log it."""

    communication: Communication
    mailbox_address: str
    received_at: datetime
    sent_at: datetime | None
    conversation_id: str | None
    change_key: str | None
    parent_folder_id: str | None
    is_draft: bool

    @property
    def from_address(self) -> str | None:
        for p in self.communication.participants:
            if p.role is ParticipantRole.FROM:
                return p.address
        return None

    @property
    def participant_addresses(self) -> set[str]:
        return {p.address for p in self.communication.participants if p.address}


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _text(part: dict[str, Any] | None) -> str:
    if not part:
        return ""
    return str(part.get("content") or "")


def _addresses(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [i["emailAddress"] for i in items if i and i.get("emailAddress")]


def normalize_message(
    raw: dict[str, Any], *, connection_ref: str, mailbox_id: str, mailbox_address: str
) -> EmailMessage:
    mailbox_address = mailbox_address.lower()
    participants: list[Participant] = []
    seen: set[tuple[str, str]] = set()
    for key, role in _ROLES:
        for addr in _addresses(raw.get(key)):
            address = (addr.get("address") or "").strip().lower() or None
            if address is None or (address, role.value) in seen:
                continue
            seen.add((address, role.value))
            participants.append(
                Participant(role=role, address=address, display_name=addr.get("name") or None)
            )
    from_address = next((p.address for p in participants if p.role is ParticipantRole.FROM), None)
    direction = Direction.OUTBOUND if from_address == mailbox_address else Direction.INBOUND

    # Index the new text only: uniqueBody drops quoted history, which is already
    # indexed with the earlier messages in the thread.
    body = _text(raw.get("uniqueBody")).strip() or _text(raw.get("body")).strip()

    attachments = tuple(
        AttachmentMetadata(
            native_id=a["id"],
            name=a.get("name"),
            content_type=a.get("contentType"),
            size_bytes=int(a.get("size") or 0),
            is_inline=bool(a.get("isInline")),
            content_id=a.get("contentId"),
        )
        for a in raw.get("attachments") or []
    )
    received = _time(raw.get("receivedDateTime"))
    if received is None:
        raise ValueError("message has no receivedDateTime")
    communication = Communication(
        provider=Provider.EMAIL,
        connection_ref=connection_ref,
        scope=SourceScopeRef(scope_kind=ScopeKind.MAILBOX, native_scope_id=mailbox_id),
        native_id=raw["id"],
        source_revision=raw.get("changeKey"),
        kind=CommunicationKind.EMAIL,
        occurred_at=received,
        title=raw.get("subject") or None,
        body_text=body,
        direction=direction,
        participants=tuple(participants),
        attachments=attachments,
        thread_ref=raw.get("conversationId"),
        internet_message_id=raw.get("internetMessageId"),  # exactly as delivered
        source_url=raw.get("webLink"),
        provider_attributes={
            "has_attachments": bool(raw.get("hasAttachments")),
            "attachment_count": len(attachments),
        },
    )
    return EmailMessage(
        communication=communication,
        mailbox_address=mailbox_address,
        received_at=received,
        sent_at=_time(raw.get("sentDateTime")),
        conversation_id=raw.get("conversationId"),
        change_key=raw.get("changeKey"),
        parent_folder_id=raw.get("parentFolderId"),
        is_draft=bool(raw.get("isDraft")),
    )


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def source_revision(msg: EmailMessage) -> str:
    """sha256 of the canonical normalized payload (communication_versions.source_revision)."""
    c = msg.communication
    payload = {
        "v": NORMALIZER_VERSION,
        "subject": c.title,
        "body": c.body_text,
        "participants": [(p.role.value, p.address) for p in c.participants],
        "attachments": [a.model_dump() for a in c.attachments],
        "sent_at": msg.sent_at,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def content_fingerprint(msg: EmailMessage) -> str | None:
    """Conservative fingerprint for correlating copies (docs/EMAIL_DEDUPLICATION.md).

    Covers from, sent time, subject, whitespace-normalized body and the
    attachment manifest; never recipients or BCC visibility. In E1 attachment
    bytes are not fetched, so a message with attachments gets no fingerprint:
    a complete fingerprint cannot be asserted from missing bytes.
    """
    c = msg.communication
    if c.attachments:
        return None
    body = re.sub(r"\s+", " ", c.body_text or "").strip()
    payload = {
        "v": FINGERPRINT_VERSION,
        "from": msg.from_address,
        "sent_at": msg.sent_at,
        "subject": c.title,
        "body": body,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()
