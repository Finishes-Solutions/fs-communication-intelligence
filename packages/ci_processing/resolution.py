"""Has an action item been dealt with? Judged from the replies in its email thread.

An action item is an email the classifier marked ``action_required`` or
``approval_required``. When a later email arrives in the same conversation, a
separate small model call (ci_processing.azure.AzureOpenAIActionResolver) reads
the original request and the replies, newest last, and answers ``resolved``
(the thing asked for was done, approved, sent, paid or explicitly closed),
``in_progress`` (someone took it on but it is not finished) or ``open``
(nothing in the replies settles it). The answer is an AI proposal, uncalibrated
until reviewed. Email text is untrusted evidence: instructions inside it are
never followed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

RESOLUTION_VERSION = "action-resolution-v1"
STATUSES = ("resolved", "in_progress", "open")
MAX_REQUEST_CHARS = 3_000
MAX_REPLY_CHARS = 2_500
MAX_REPLIES = 6
MAX_REASON_CHARS = 160

CRITERIA = """resolved: a reply shows the request was done or closed: the approval was given or \
refused, the document was signed or sent, the payment was made or released, the answer was \
given, the meeting was set, or the requester said it is no longer needed. A reply that merely \
acknowledges ("got it", "will look") is not resolved.
in_progress: a reply shows someone is working on it or asked a clarifying question, but the \
request itself is not yet done.
open: the replies do not settle the request (no reply from anyone who could act, a reminder, \
an unrelated message, or more of the same request)."""


@dataclass(frozen=True)
class Reply:
    communication_id: str
    sender: str | None
    received_at: str  # ISO 8601
    subject: str | None
    body_text: str


@dataclass(frozen=True)
class ResolutionInput:
    subject: str
    body_text: str
    action_text: str | None
    replies: tuple[Reply, ...]

    def sha256(self) -> str:
        """Identical copies of one thread (several mailboxes) share one call."""
        parts = [
            RESOLUTION_VERSION,
            self.subject,
            self.body_text[:MAX_REQUEST_CHARS],
            self.action_text or "",
        ]
        for r in self.replies[-MAX_REPLIES:]:
            parts += [r.sender or "", r.received_at, r.body_text[:MAX_REPLY_CHARS]]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class Resolution:
    status: str
    confidence: float | None
    reason: str | None
    resolver_index: int | None  # index into the replies given, or None
    model: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError("status must be resolved, in_progress or open")


class ActionResolver(Protocol):
    model: str

    def resolve(self, item: ResolutionInput) -> Resolution: ...


def clean_reason(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text[:MAX_REASON_CHARS] or None


def clean_index(value: object, count: int) -> int | None:
    """A reply index the model gave, or None when out of range or negative."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value < count else None


class FakeActionResolver:
    """Deterministic keyword rules for tests and local runs (no AI)."""

    model = "fake-action-resolver"
    _RESOLVED = re.compile(
        r"\b(approved|approve this|signed copy|sent (it|the)|has been (paid|sent|approved)|"
        r"payment (was )?(made|released)|done\b|completed|no longer needed|closing this)\b",
        re.I,
    )
    _PROGRESS = re.compile(
        r"\b(working on|will (send|review|get)|looking into|in progress)\b", re.I
    )

    def resolve(self, item: ResolutionInput) -> Resolution:
        replies = item.replies[-MAX_REPLIES:]
        for index in range(len(replies) - 1, -1, -1):
            text = replies[index].body_text[:MAX_REPLY_CHARS]
            if self._RESOLVED.search(text):
                return Resolution("resolved", 0.9, "a reply says it was done", index, self.model)
        for index in range(len(replies) - 1, -1, -1):
            if self._PROGRESS.search(replies[index].body_text[:MAX_REPLY_CHARS]):
                return Resolution(
                    "in_progress", 0.7, "a reply says it is being worked on", index, self.model
                )
        return Resolution("open", 0.6, "no reply settles the request", None, self.model)
