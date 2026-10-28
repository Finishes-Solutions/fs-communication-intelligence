"""Cheap rules that skip obvious bulk and automated mail before any paid step.

Approved by Matthew on 2026-10-28 to cut cost: a message that matches is
recorded as ``not_indexed`` (the same outcome the classifier gives spam and
newsletters) without screening, masking, classification or embedding.

The rules are deliberately narrow, because a skipped message is never
searchable:

- ``automated_sender``: the sender's address starts with no-reply,
  notifications, alerts and the like (the same pattern the action-item rules use).
- ``bulk_unsubscribe``: the body has an unsubscribe link or line, and the
  subject is not a reply or forward (a person forwarding a newsletter with a
  question is kept).

Never skipped: mail with a real attachment (invoices, pay apps and scans often
come from no-reply addresses; signature images and logos do not count), and
mail from a sender domain listed in ``BULK_PREFILTER_ALLOW_DOMAINS`` (e.g. a
project-management system whose notifications carry project content).
``ci process --retry-skipped`` reprocesses skipped mail in full.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from ci_processing.routing import AUTOMATED_SENDER

PREFILTER_VERSION = "prefilter-2026-10-28"
REASONS = ("automated_sender", "bulk_unsubscribe")

_UNSUBSCRIBE = re.compile(
    r"\bunsubscribe\b|\b(email|subscription|communication) preferences\b",
    re.IGNORECASE,
)
_REPLY_OR_FORWARD = re.compile(r"^\s*(re|fw|fwd)\s*:", re.IGNORECASE)


def allow_domains(value: str | None) -> frozenset[str]:
    """``BULK_PREFILTER_ALLOW_DOMAINS``: comma-separated domains, ``@`` optional."""
    return frozenset(d.strip().lower().lstrip("@") for d in (value or "").split(",") if d.strip())


def _domain_allowed(sender: str, allowed: Collection[str]) -> bool:
    domain = sender.rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d) for d in allowed)


def bulk_skip_reason(
    *,
    sender: str,
    subject: str,
    body: str,
    has_real_attachment: bool,
    allowed_domains: Collection[str] = (),
) -> str | None:
    """The reason code to skip this message, or None to process it in full."""
    sender = sender.strip().lower()
    if has_real_attachment or not sender or _domain_allowed(sender, allowed_domains):
        return None
    if AUTOMATED_SENDER.match(sender):
        return "automated_sender"
    if not _REPLY_OR_FORWARD.match(subject or "") and _UNSUBSCRIBE.search(body):
        return "bulk_unsubscribe"
    return None
