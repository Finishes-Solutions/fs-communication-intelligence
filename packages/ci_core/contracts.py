"""Source-neutral communication contract (version 1).

Every adapter (email now, Teams later) normalizes source records into a
``Communication``. Nothing here assumes a mailbox, subject or recipient list;
email-specific consistency is checked only when ``kind`` is ``email``.

The contract carries content (``body_text``), so instances must never be
logged. Log IDs, counts, states and reason codes only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Provider(StrEnum):
    EMAIL = "email"
    TEAMS = "teams"
    FIREFLIES = "fireflies"
    DIALPAD = "dialpad"


class CommunicationKind(StrEnum):
    EMAIL = "email"
    CHAT_MESSAGE = "chat_message"
    CHANNEL_MESSAGE = "channel_message"
    MEETING = "meeting"
    CALL = "call"


class ScopeKind(StrEnum):
    MAILBOX = "mailbox"
    TEAMS_CHAT = "teams_chat"
    TEAMS_CHANNEL = "teams_channel"
    FIREFLIES_USER = "fireflies_user"
    DIALPAD_USER = "dialpad_user"


class ParticipantRole(StrEnum):
    FROM = "from"
    SENDER = "sender"
    TO = "to"
    CC = "cc"
    BCC = "bcc"
    AUTHOR = "author"
    MENTIONED = "mentioned"
    ORGANIZER = "organizer"
    ATTENDEE = "attendee"
    SPEAKER = "speaker"
    CALLER = "caller"
    CALLEE = "callee"


class Direction(StrEnum):
    INBOUND = "in"
    OUTBOUND = "out"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


_KINDS_BY_PROVIDER: dict[Provider, frozenset[CommunicationKind]] = {
    Provider.EMAIL: frozenset({CommunicationKind.EMAIL}),
    Provider.TEAMS: frozenset(
        {
            CommunicationKind.CHAT_MESSAGE,
            CommunicationKind.CHANNEL_MESSAGE,
            CommunicationKind.MEETING,
        }
    ),
    Provider.FIREFLIES: frozenset({CommunicationKind.MEETING}),
    Provider.DIALPAD: frozenset({CommunicationKind.CALL, CommunicationKind.CHAT_MESSAGE}),
}

_SCOPES_BY_PROVIDER: dict[Provider, frozenset[ScopeKind]] = {
    Provider.EMAIL: frozenset({ScopeKind.MAILBOX}),
    Provider.TEAMS: frozenset({ScopeKind.TEAMS_CHAT, ScopeKind.TEAMS_CHANNEL}),
    Provider.FIREFLIES: frozenset({ScopeKind.FIREFLIES_USER}),
    Provider.DIALPAD: frozenset({ScopeKind.DIALPAD_USER}),
}

# Content-free provider metadata only: identifiers, flags, counts.
ProviderAttribute = str | int | bool | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SourceScopeRef(_Strict):
    """The scope a copy was captured from, such as one mailbox (Entra object ID)."""

    scope_kind: ScopeKind
    native_scope_id: str = Field(min_length=1)


class Participant(_Strict):
    role: ParticipantRole
    address: str | None = None
    display_name: str | None = None
    native_id: str | None = None

    @model_validator(mode="after")
    def _has_identity(self) -> Self:
        if not (self.address or self.native_id):
            raise ValueError("a participant needs an address or a native_id")
        return self


class AttachmentMetadata(_Strict):
    """E1 records attachment metadata only; no content is fetched or stored."""

    native_id: str = Field(min_length=1)
    name: str | None = None
    content_type: str | None = None
    size_bytes: int = Field(ge=0)
    is_inline: bool = False
    content_id: str | None = None


class Communication(_Strict):
    contract_version: Literal["1"] = "1"
    provider: Provider
    connection_ref: str = Field(min_length=1, description="Tenant or account the copy came from")
    scope: SourceScopeRef
    native_id: str = Field(min_length=1, description="Immutable source ID of this copy")
    source_revision: str | None = Field(default=None, description="Change key or equivalent")
    kind: CommunicationKind
    occurred_at: datetime
    title: str | None = None
    body_text: str | None = None
    direction: Direction = Direction.UNKNOWN
    participants: tuple[Participant, ...] = ()
    attachments: tuple[AttachmentMetadata, ...] = ()
    thread_ref: str | None = None
    internet_message_id: str | None = None
    source_url: str | None = None
    provider_attributes: dict[str, ProviderAttribute] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind not in _KINDS_BY_PROVIDER[self.provider]:
            raise ValueError(f"kind {self.kind} is not valid for provider {self.provider}")
        if self.scope.scope_kind not in _SCOPES_BY_PROVIDER[self.provider]:
            raise ValueError(
                f"scope kind {self.scope.scope_kind} is not valid for provider {self.provider}"
            )
        if self.internet_message_id is not None and self.kind is not CommunicationKind.EMAIL:
            raise ValueError("internet_message_id is only valid for email")
        return self
