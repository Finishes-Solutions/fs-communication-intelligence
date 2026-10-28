"""Masking of sensitive numbers and credentials before anything is classified,
embedded, stored as evidence or shown (Matthew, 2026-10-28: mask instead of
restricting the whole email; CLAUDE.md section 3a).

Every redactor applies :func:`mask_credentials` (passwords, one-time codes,
API keys, tokens, links with query strings). The Azure redactor
(ci_processing.azure) adds Azure AI Language PII detection for government IDs,
bank, routing and card numbers; the fake one adds simple regexes for local
development. Counts name categories only, never content. A dependency failure
raises :class:`RedactionUnavailable`, and the message is held, never published
unmasked.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

CREDENTIAL = "[credential]"
MASKED = "[masked]"


class RedactionUnavailable(RuntimeError):
    """The masking service did not answer; the message must not be published."""


@dataclass(frozen=True)
class Redaction:
    texts: list[str]
    counts: dict[str, int] = field(default_factory=dict)

    def reason_code(self) -> str | None:
        """``masked:CreditCardNumber=1,credential=2``, or None when nothing was masked."""
        if not self.counts:
            return None
        return "masked:" + ",".join(f"{k}={v}" for k, v in sorted(self.counts.items()))


class Redactor(Protocol):
    def redact(self, texts: Sequence[str]) -> Redaction: ...


_INTRODUCERS = (  # words that introduce a credential value
    r"passwords?|passcodes?|passwd|pwd|pin|one[- ]time (?:pass)?code|one[- ]time password"
    r"|verification code|security code|access code|otp|api[ _-]?key|secret|token"
)
# "password: hunter2", "your PIN is 4821", "API key = abc123"
# The value stops before trailing punctuation and skips what is already masked.
_VALUE = r"(?!\[(?:credential|masked)\])([^\s,;]+?)(?=[.,;:!?)]?(?:\s|$))"
_LABELLED_SECRET = re.compile(rf"(?i)\b({_INTRODUCERS})\b(\s*(?::|=|\bis\b)\s*){_VALUE}")
# "code: 1234", "Your code is 123456", "use code 123456" (bare form: 6-8 digits, so
# "zip code 77484" stays).
_CODE = re.compile(r"(?i)\b(code)\b(\s*(?::|\bis\b)\s*(?=\d{4,10}\b)|\s+(?=\d{6,8}\b))(\d+)\b")
_KEY_FORMATS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}",
    re.DOTALL,
)
# Keep scheme, host and path; drop the query (reset links, signed URLs, tokens).
_URL_QUERY = re.compile(r"(https?://[^\s?#<>\"']+)\?[^\s<>\"')\]]+")


def mask_credentials(text: str) -> tuple[str, int]:
    """Mask credential-like values; returns the text and how many were masked."""
    count = 0

    def labelled(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}{CREDENTIAL}"

    def whole(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return CREDENTIAL

    def query(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}?{MASKED}"

    text = _URL_QUERY.sub(query, text)
    text = _KEY_FORMATS.sub(whole, text)
    text = _LABELLED_SECRET.sub(labelled, text)
    text = _CODE.sub(labelled, text)
    return text, count


def mask_all(texts: Sequence[str], counts: Counter[str]) -> list[str]:
    out = []
    for text in texts:
        masked, n = mask_credentials(text)
        if n:
            counts["credential"] += n
        out.append(masked)
    return out


_FAKE_PII = (
    ("USSocialSecurityNumber", re.compile(r"()\b\d{3}-\d{2}-\d{4}\b")),
    ("CreditCardNumber", re.compile(r"()\b(?:\d{4}[ -]?){3}\d{4}\b")),
    (
        "USBankAccountNumber",
        re.compile(r"(?i)(\b(?:routing|account) (?:number|no\.?|#)\s*[:#]?\s*)\d{6,17}\b"),
    ),
)


def _keep_prefix(label: str) -> Callable[[re.Match[str]], str]:
    return lambda m: m.group(1) + label


class FakeRedactor:
    """Regex redactor for local development (``SCREENER=fake``)."""

    def redact(self, texts: Sequence[str]) -> Redaction:
        counts: Counter[str] = Counter()
        out = []
        for text in mask_all(texts, counts):
            for category, pattern in _FAKE_PII:
                text, n = pattern.subn(_keep_prefix(f"[{category}]"), text)
                if n:
                    counts[category] += n
            out.append(text)
        return Redaction(out, dict(counts))
