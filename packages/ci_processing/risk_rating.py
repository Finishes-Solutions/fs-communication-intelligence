"""Impact and likelihood of a project risk, for the Risk page's 3x3 matrix.

Only emails the classifier marked ``project_risk`` are rated, by a separate small
model call (ci_processing.azure.AzureOpenAIRiskRater). The criteria below are
the whole definition of the ratings; they are AI proposals, not assessments,
and are uncalibrated until reviewed. Email text is untrusted evidence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

RATING_VERSION = "risk-rating-v1"
LEVELS = ("low", "medium", "high")
MAX_BODY_CHARS = 8_000
MAX_REASON_CHARS = 160

CRITERIA = """Impact (how much harm to the project or property if it happens):
- high: a lien, claim, lawsuit or legal notice; a safety incident or injury; financing, a \
loan draw or closing at risk; a failed inspection or permit problem that stops work or \
occupancy; a delay of more than two weeks or to the critical path; a cost increase over about \
$50,000 or more than 5% of a budget; a key party stopping work or defaulting.
- medium: a delay of a few days to two weeks; a cost increase of about $5,000-$50,000; rework, \
a quality defect or a permit issue that can be fixed; a dispute that is still being negotiated.
- low: a minor delay, a small cost, a fixable defect, or a concern with limited consequence.

Likelihood (how likely the harm is to happen, judged only from the email):
- high: it is already happening or confirmed (inspection failed, lien filed, notice served, \
payment missed, work stopped, deadline already missed).
- medium: an explicit warning or threat ("will file", "at risk of", "cannot meet the date \
unless").
- low: a possibility, a question or a general concern without a firm sign it will happen."""


@dataclass(frozen=True)
class RiskInput:
    subject: str
    body_text: str
    risk_text: str | None = None

    def sha256(self) -> str:
        """Identical copies of one email (several mailboxes) share one rating."""
        payload = "\x1f".join(
            (RATING_VERSION, self.subject, self.body_text[:MAX_BODY_CHARS], self.risk_text or "")
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class RiskRating:
    impact: str
    likelihood: str
    reason: str | None
    model: str

    def __post_init__(self) -> None:
        if self.impact not in LEVELS or self.likelihood not in LEVELS:
            raise ValueError("impact and likelihood must be low, medium or high")


class RiskRater(Protocol):
    model: str

    def rate(self, item: RiskInput) -> RiskRating: ...


def clean_reason(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    return text[:MAX_REASON_CHARS] or None


class FakeRiskRater:
    """Deterministic keyword rules for tests and local runs (no AI)."""

    model = "fake-risk-rater"
    _HIGH_IMPACT = re.compile(
        r"\b(lien|lawsuit|legal|safety|injur\w*|default|financ\w*|draw)\b", re.I
    )
    _MEDIUM_IMPACT = re.compile(
        r"\b(delay\w*|defect\w*|rework|inspection|change order|cost)\b", re.I
    )
    _HIGH_LIKELY = re.compile(r"\b(failed|filed|served|missed|stopped|notice of)\b", re.I)
    _MEDIUM_LIKELY = re.compile(r"\b(will|intent|at risk|unless|warning|threat\w*)\b", re.I)

    def rate(self, item: RiskInput) -> RiskRating:
        text = f"{item.subject}\n{item.body_text}\n{item.risk_text or ''}"
        impact = (
            "high"
            if self._HIGH_IMPACT.search(text)
            else "medium"
            if self._MEDIUM_IMPACT.search(text)
            else "low"
        )
        likelihood = (
            "high"
            if self._HIGH_LIKELY.search(text)
            else "medium"
            if self._MEDIUM_LIKELY.search(text)
            else "low"
        )
        return RiskRating(impact, likelihood, clean_reason(item.risk_text), self.model)
