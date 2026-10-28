"""What happens to a message after screening and classification.

Order (first match wins):

1. Screening: any ``reject`` -> restricted; any ``pending`` -> held
   (``screening_pending``); any ``hold`` -> held with that reason. Classification
   runs only when every check allows.
2. CLAUDE.md section 7: credentials, potential_phi or sensitive_pii = yes ->
   restricted (not embedded, never returned).
3. Any of those three uncertain (review or unresolved), or phishing not ``no``
   -> review (indexed and flagged).
4. spam or newsletter = yes -> not_indexed (recorded only).
5. Otherwise index.

Holding a detected prompt attack (instead of indexing it flagged) is a
provisional choice: the docs say rejected content must not reach embeddings,
and CLAUDE.md does not settle prompt attacks. Revisit in PR 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ci_processing.classification import ClassificationResult, Decision
from ci_processing.screening import CheckDecision, ScreeningResult

RESTRICTING_LABELS = ("credentials", "potential_phi", "sensitive_pii")


class Disposition(StrEnum):
    INDEX = "index"
    REVIEW = "review"
    NOT_INDEXED = "not_indexed"
    RESTRICTED = "restricted"
    HELD = "held"

    @property
    def embedded(self) -> bool:
        return self in (Disposition.INDEX, Disposition.REVIEW)


@dataclass(frozen=True)
class Outcome:
    disposition: Disposition
    reason_codes: tuple[str, ...] = ()


def screening_gate(result: ScreeningResult) -> Outcome | None:
    """An outcome when screening stops processing, else ``None`` (go on to classify)."""
    by_decision: dict[CheckDecision, list[str]] = {}
    for check in result.checks:
        by_decision.setdefault(check.decision, []).append(check.reason_code or check.check_name)
    if rejected := by_decision.get(CheckDecision.REJECT):
        return Outcome(Disposition.RESTRICTED, tuple(sorted(rejected)))
    if by_decision.get(CheckDecision.PENDING):
        return Outcome(Disposition.HELD, ("screening_pending",))
    if held := by_decision.get(CheckDecision.HOLD):
        return Outcome(Disposition.HELD, tuple(sorted(held)))
    return None


def classification_outcome(result: ClassificationResult) -> Outcome:
    decisions = result.decisions()
    restricted = [n for n in RESTRICTING_LABELS if decisions[n] is Decision.YES]
    if restricted:
        return Outcome(Disposition.RESTRICTED, tuple(restricted))
    review = [n for n in RESTRICTING_LABELS if decisions[n] is not Decision.NO]
    if decisions["phishing"] is not Decision.NO:
        review.append("phishing")
    if review:
        return Outcome(Disposition.REVIEW, tuple(review))
    bulk = [n for n in ("spam", "newsletter") if decisions[n] is Decision.YES]
    if bulk:
        return Outcome(Disposition.NOT_INDEXED, tuple(bulk))
    return Outcome(Disposition.INDEX)
