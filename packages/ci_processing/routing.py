"""Step 2 of action items: is this email an action item for one specific person?

Step 1 (the classifier) tags whether an email asks *someone* to act. Step 2
runs per configured person (``ci.action_people``) and asks a separate model
call whether it is *this person's* action, given an administrator-written
profile: name, addresses, role and who they must not be confused with (Chuck
Sr. is not Chuck Jr.). Only plausible candidates reach the model: step 1 said
yes or was unsure, the mail is not possible bulk, not from an automated sender
and not sent by the person, and the person received it (to/cc) or it sits in
one of their mailboxes. Everyone else gets a rule-based ``no`` so every
decision has a reason.

The email is untrusted input; the profile is trusted configuration. The result
is a proposal with a probability, never an approval, and its thresholds are
calibrated from reviewer answers in the admin console (Label check).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ci_processing.classification import ClassificationInput, ClassificationResult, Decision

ROUTING_VERSION = "person-route-2026-10-28-uncalibrated"
NO_AT_OR_BELOW = 0.30
YES_AT_OR_ABOVE = 0.70
STEP1_LABELS = ("action_required", "approval_required")
BULK_LABELS = ("newsletter", "spam")
AUTOMATED_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer-daemon"
    r"|postmaster|bounces?|automated|system)[^@]*@"
)
YES_REASONS = ("named_directly", "sole_recipient", "role_responsibility")
NO_REASONS = (
    "someone_else",
    "namesake",
    "group_request",
    "cc_only",
    "fyi_only",
    "automated",
    "no_request",
    "sent_by_person",
    "not_a_recipient",
)
REASONS = YES_REASONS + NO_REASONS


@dataclass(frozen=True)
class PersonProfile:
    key: str  # e.g. chuck_sr
    display_name: str
    addresses: tuple[str, ...]
    role: str = ""
    not_to_confuse: str = ""  # e.g. "Chuck Jr. (cjr@example.com), operations; also called Chuck"


@dataclass(frozen=True)
class Route:
    probability: float | None
    action: str | None
    reason: str | None
    model: str

    @property
    def decision(self) -> Decision:
        if self.probability is None:
            return Decision.UNRESOLVED
        if self.probability <= NO_AT_OR_BELOW:
            return Decision.NO
        if self.probability >= YES_AT_OR_ABOVE:
            return Decision.YES
        return Decision.REVIEW


class PersonRouter(Protocol):
    model: str

    def route(self, item: ClassificationInput, person: PersonProfile) -> Route: ...


def rule_out(
    result: ClassificationResult,
    item: ClassificationInput,
    person: PersonProfile,
    mailbox: str = "",
) -> str | None:
    """A reason code when the rules already say "not this person's", else None."""
    decisions = result.decisions()
    if all(decisions[n] is Decision.NO for n in STEP1_LABELS):
        return "no_request"
    if any(decisions[n] is not Decision.NO for n in BULK_LABELS):
        return "fyi_only"
    sender = item.sender.lower()
    if AUTOMATED_SENDER.match(sender):
        return "automated"
    mine = {a.lower() for a in person.addresses}
    if sender in mine:
        return "sent_by_person"
    recipients = {a.lower() for a in (*item.to, *item.cc)}
    if not (recipients & mine) and mailbox.lower() not in mine:
        return "not_a_recipient"
    return None


class FakePersonRouter:
    """Deterministic stand-in for local development and tests."""

    model = "fake-person-router-v1"

    def route(self, item: ClassificationInput, person: PersonProfile) -> Route:
        mine = {a.lower() for a in person.addresses}
        to = {a.lower() for a in item.to}
        first_name = person.display_name.split()[0].lower() if person.display_name else ""
        greeting = item.body_text[:200].lower()
        if not to & mine:
            return Route(0.05, None, "cc_only", self.model)
        if len(to) == 1 or (first_name and re.search(rf"\b{re.escape(first_name)}\b", greeting)):
            return Route(0.9, "respond to: " + item.subject[:80], "named_directly", self.model)
        return Route(0.5, None, "group_request", self.model)
