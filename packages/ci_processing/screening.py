"""Content screening (in-tenant): PII detection and prompt-attack shields.

Deployed backends are Azure AI Language PII detection and Content Safety
Prompt Shields (not built yet). A dependency failure means pending, never
pass: the screener raises ``ScreeningUnavailable`` and the job retries.

The fake screener uses patterns over synthetic fixtures only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from ci_core.settings import ScreenerBackend

Decision = Literal["allow", "hold"]


class ScreeningUnavailable(Exception):
    code = "screening_unavailable"


@dataclass(frozen=True)
class ScreeningCheck:
    check_name: str  # policy_decisions.check_name
    decision: Decision
    reason_code: str | None
    service_ref: str


class Screener(Protocol):
    def screen(self, text: str) -> list[ScreeningCheck]: ...


_SENSITIVE = (
    (r"\b\d{3}-\d{2}-\d{4}\b", "sensitive_pii_ssn"),
    (r"\b(?:\d[ -]?){15,16}\b", "sensitive_pii_card"),
    (r"routing (number|no\.?)\s*[:#]?\s*\d{9}\b", "sensitive_pii_bank"),
)
_ATTACKS = (
    r"ignore (all )?(previous|prior|above) instructions",
    r"disregard (your|the) (system|previous) (prompt|instructions)",
    r"you are now (an?|the) ",
)


class FakeScreener:
    """``unavailable=True`` simulates the screening service being down."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    def screen(self, text: str) -> list[ScreeningCheck]:
        if self.unavailable:
            raise ScreeningUnavailable("simulated screening outage")
        lowered = text.lower()
        pii_reason = next(
            (code for pattern, code in _SENSITIVE if re.search(pattern, lowered)), None
        )
        attack = any(re.search(p, lowered) for p in _ATTACKS)
        return [
            ScreeningCheck(
                "pii_screen",
                "hold" if pii_reason else "allow",
                f"restricted_{pii_reason}" if pii_reason else None,
                "fake-pii-patterns.v1",
            ),
            # Provisional: a detected prompt attack holds the item (not decided yet).
            ScreeningCheck(
                "prompt_shield",
                "hold" if attack else "allow",
                "prompt_attack_detected" if attack else None,
                "fake-prompt-shield.v1",
            ),
        ]


def make_screener(backend: ScreenerBackend, *, unavailable: bool = False) -> Screener:
    if backend is ScreenerBackend.FAKE:
        return FakeScreener(unavailable=unavailable)
    raise NotImplementedError("SCREENER=azure is not built yet (PR 6); use fake locally")
