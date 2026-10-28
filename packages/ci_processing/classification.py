"""Classifier interface, the CLAUDE.md section 7 label table, and the fake backend.

A classifier returns one probability per label (or ``None`` when it gave no
answer). Thresholds turn that into a decision: ``no`` at or below the "no"
threshold, ``yes`` at or above the "yes" threshold, ``review`` in between and
``unresolved`` when the answer is missing (never treated as 0). The output is
a proposal with provenance, never an approval.

The thresholds are provisional and uncalibrated. Calibrate them on a labelled
pilot sample before anything depends on them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

POLICY_VERSION = "labels-2026-10-14-uncalibrated"


class Decision(StrEnum):
    NO = "no"
    YES = "yes"
    REVIEW = "review"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Label:
    name: str
    no_at_or_below: float
    yes_at_or_above: float
    question: str

    def decide(self, probability: float | None) -> Decision:
        if probability is None:
            return Decision.UNRESOLVED
        if probability <= self.no_at_or_below:
            return Decision.NO
        if probability >= self.yes_at_or_above:
            return Decision.YES
        return Decision.REVIEW


LABELS: tuple[Label, ...] = (
    Label("spam", 0.15, 0.95, "Unsolicited bulk junk or irrelevant mass promotion?"),
    Label(
        "phishing", 0.10, 0.80, "Phishing, impersonation, scam or payment-redirection indicators?"
    ),
    Label("pii", 0.20, 0.80, "Identifies a person, including routine business contact details?"),
    Label(
        "sensitive_pii", 0.10, 0.75, "SSNs, government IDs, bank/card numbers, payroll or similar?"
    ),
    Label("potential_phi", 0.10, 0.75, "Identifiable health information?"),
    Label(
        "credentials", 0.10, 0.75, "Actual secrets, passwords, codes, API keys or credential links?"
    ),
    Label("confidential_business", 0.15, 0.80, "Substantive nonpublic business information?"),
    Label(
        "project_related", 0.20, 0.80, "About a specific project, property, asset or opportunity?"
    ),
    Label(
        "invoice_receipt", 0.20, 0.80, "A specific invoice, receipt, pay application or payment?"
    ),
    Label("approval_required", 0.20, 0.80, "Requests a pending decision from the mailbox owner?"),
    Label("action_required", 0.20, 0.80, "Asks the mailbox owner for a concrete pending action?"),
    Label("urgent", 0.15, 0.85, "Credible unresolved need due today or next business day?"),
    Label("newsletter", 0.20, 0.85, "Newsletter, digest or informational mass mailing?"),
    Label(
        "cat_development_construction",
        0.20,
        0.70,
        "Design, permits, inspections, contractors, schedule, scope, change orders?",
    ),
    Label("cat_payment_billing", 0.20, 0.70, "Invoices, pay apps, draws, payment status?"),
    Label(
        "cat_leasing_operations",
        0.20,
        0.70,
        "Leasing, occupancy, tenants, property management/operations?",
    ),
    Label(
        "cat_compliance_legal", 0.20, 0.70, "Compliance, legal, insurance, regulatory, disputes?"
    ),
    Label(
        "cat_capital_finance",
        0.20,
        0.70,
        "Capital, lenders, loans, investors, refinancing, acquisitions?",
    ),
    Label("cat_vendor_performance", 0.20, 0.70, "Vendor quality, delays, defects, complaints?"),
    Label(
        "cat_general_governance", 0.20, 0.70, "Internal approvals, policies, committee decisions?"
    ),
)
LABELS_BY_NAME: Mapping[str, Label] = {label.name: label for label in LABELS}


@dataclass(frozen=True)
class ClassificationInput:
    """Untrusted source text. Instructions inside it must be ignored by every backend."""

    subject: str
    body_text: str

    def sha256(self) -> str:
        canonical = json.dumps([self.subject, self.body_text], ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ClassificationResult:
    model: str
    policy_version: str
    input_sha256: str
    probabilities: Mapping[str, float | None]

    def decisions(self) -> dict[str, Decision]:
        """Every label gets a decision; a label the backend did not answer is unresolved."""
        return {label.name: label.decide(self.probabilities.get(label.name)) for label in LABELS}

    def yes_labels(self) -> set[str]:
        return {name for name, d in self.decisions().items() if d is Decision.YES}


class Classifier(Protocol):
    def classify(self, item: ClassificationInput) -> ClassificationResult: ...


# Keyword rules for local development. They exist so fixtures exercise every
# decision path; they say nothing about real classification quality.
_FAKE_RULES: tuple[tuple[str, str, float], ...] = (
    ("spam", r"you have been selected|claim your prize|limited-time offer|act now", 0.97),
    (
        "phishing",
        r"updated bank(ing)? details|new (bank )?account|verify your (account|password)",
        0.9,
    ),
    ("newsletter", r"newsletter|weekly digest|unsubscribe", 0.9),
    ("pii", r"\b\d{3}-\d{3}-\d{4}\b|\bssn\b", 0.9),
    ("sensitive_pii", r"\b\d{3}-\d{2}-\d{4}\b|routing number|card number", 0.95),
    ("potential_phi", r"diagnosis|medical leave|prescription", 0.9),
    ("credentials", r"(api[_ -]?key|password|secret)\s*[:=]|\bsk-[a-z0-9]{16,}", 0.95),
    ("confidential_business", r"confidential|term sheet|letter of intent|draw request|loan", 0.85),
    (
        "project_related",
        r"\blot \d+|phase \d|building [a-z]\b|suite \d+|example (ridge|commons)",
        0.9,
    ),
    ("invoice_receipt", r"invoice|receipt|pay application|remittance", 0.9),
    ("approval_required", r"confirm approval|approve|sign off", 0.9),
    (
        "action_required",
        r"please (confirm|send|review|forward)|by (monday|tuesday|wednesday|thursday|friday)",
        0.88,
    ),
    ("urgent", r"\burgent\b|\basap\b|\btoday\b|within 24 hours", 0.9),
    (
        "cat_development_construction",
        r"change order|permit|inspection|grading|framing|plumbing|foundation|pour",
        0.85,
    ),
    ("cat_payment_billing", r"invoice|pay application|payment|remittance", 0.85),
    ("cat_leasing_operations", r"lease|tenant|occupancy|property management", 0.85),
    ("cat_compliance_legal", r"lien|attorney|legal|insurance|dispute|compliance", 0.85),
    ("cat_capital_finance", r"lender|\bloan\b|draw request|investor|refinanc", 0.85),
    ("cat_vendor_performance", r"defects?|delays?|missed|complaint", 0.85),
    ("cat_general_governance", r"committee|policy|board approval", 0.85),
)
_COMPILED = tuple((name, re.compile(p, re.IGNORECASE), prob) for name, p, prob in _FAKE_RULES)
_FAKE_NO = 0.03


class FakeClassifier:
    """Deterministic keyword classifier (``CLASSIFIER_BACKEND=fake``)."""

    model = "fake-keyword-rules-v1"

    def classify(self, item: ClassificationInput) -> ClassificationResult:
        text = f"{item.subject}\n{item.body_text}"
        probabilities: dict[str, float | None] = {label.name: _FAKE_NO for label in LABELS}
        for name, pattern, prob in _COMPILED:
            if pattern.search(text):
                probabilities[name] = prob
        return ClassificationResult(self.model, POLICY_VERSION, item.sha256(), probabilities)
