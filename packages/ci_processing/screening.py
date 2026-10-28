"""Screening interface (in-tenant PII detection and prompt-attack detection) and fake.

The real adapters (PR 6) call Azure AI Language PII detection and Content
Safety Prompt Shields. A dependency failure is ``pending``, never ``allow``.
Check results map onto ``ci.policy_decisions`` rows and carry no content.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CheckDecision(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    REJECT = "reject"
    PENDING = "pending"


@dataclass(frozen=True)
class CheckResult:
    check_name: str  # 'pii' | 'prompt_shield'
    decision: CheckDecision
    reason_code: str | None
    service_ref: str


@dataclass(frozen=True)
class ScreeningResult:
    checks: tuple[CheckResult, ...]

    def flagged(self) -> set[str]:
        """Reason codes of checks that did not allow, e.g. ``{'sensitive_pii'}``."""
        return {c.reason_code for c in self.checks if c.reason_code}


class Screener(Protocol):
    def screen(self, text: str) -> ScreeningResult: ...


DISABLED = "disabled_by_operator"  # service_ref of a check an operator turned off


class DisabledScreener:
    """Prompt Shields turned off in the admin console (testing only): no service is
    called, and the message records the check as skipped so it can be redone."""

    def screen(self, text: str) -> ScreeningResult:
        return ScreeningResult((CheckResult("prompt_shield", CheckDecision.ALLOW, None, DISABLED),))


_SENSITIVE_PII = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")  # US SSN shape
_PROMPT_ATTACK = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|disregard your (rules|instructions)",
    re.IGNORECASE,
)


class FakeScreener:
    """Regex screener for local development (``SCREENER=fake``).

    ``unavailable`` names checks to report as ``pending``, to simulate a
    dependency outage.
    """

    service_ref = "fake-screener-v1"

    def __init__(self, unavailable: Collection[str] = (), *, check_pii: bool = True) -> None:
        self.unavailable = frozenset(unavailable)
        self.check_pii = check_pii  # False when sensitive data is masked instead

    def screen(self, text: str) -> ScreeningResult:
        shield = self._check(
            "prompt_shield",
            _PROMPT_ATTACK.search(text) is not None,
            CheckDecision.HOLD,
            "prompt_attack",
        )
        if not self.check_pii:
            return ScreeningResult((shield,))
        pii = self._check(
            "pii", _SENSITIVE_PII.search(text) is not None, CheckDecision.REJECT, "sensitive_pii"
        )
        return ScreeningResult((pii, shield))

    def _check(self, name: str, hit: bool, decision: CheckDecision, reason: str) -> CheckResult:
        if name in self.unavailable:
            return CheckResult(
                name, CheckDecision.PENDING, "screening_unavailable", self.service_ref
            )
        if hit:
            return CheckResult(name, decision, reason, self.service_ref)
        return CheckResult(name, CheckDecision.ALLOW, None, self.service_ref)
