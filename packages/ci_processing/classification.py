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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from ci_processing.knowledge import Knowledge, LearnedName, Source, fake_extract, validate

# b: stricter action items; c: names; d: knowledge (ci_processing.knowledge)
POLICY_VERSION = "labels-2026-10-28d-uncalibrated"


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
        "project_risk",
        0.20,
        0.75,
        "Signals a risk to a specific project or property: schedule delay, cost overrun or "
        "unbudgeted cost, dispute, claim or lien, failed inspection or permit problem, safety "
        "incident, quality defect, financing or draw problem, or a key party not performing?",
    ),
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
    """Untrusted source text. Instructions inside it must be ignored by every backend.

    Mailbox-independent on purpose: copies of one message in many mailboxes
    share a hash and are classified once. "Who must act" comes back as
    ``action_for`` addresses instead of being judged per mailbox.
    """

    subject: str
    body_text: str
    sender: str = ""
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    sent: str = ""
    attachments: tuple[tuple[str, str], ...] = ()  # (name, extracted text excerpt)
    # Names the knowledge base learned from earlier mail, shown to the model as
    # untrusted reference data. Not part of the hash: it is context, not the email.
    learned: tuple[LearnedName, ...] = ()

    def sha256(self) -> str:
        canonical = json.dumps(
            [
                self.subject,
                self.body_text,
                self.sender,
                self.to,
                self.cc,
                self.sent,
                self.attachments,
            ],
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def full_text(self) -> str:
        parts = [self.subject, self.body_text, *(f"{n}\n{t}" for n, t in self.attachments)]
        return "\n".join(parts)

    def source(self) -> Source:
        """What extracted knowledge must be found in (ci_processing.knowledge.validate)."""
        addresses = {a.strip().lower() for a in (self.sender, *self.to, *self.cc) if a.strip()}
        return Source(self.full_text(), frozenset(addresses))


@dataclass(frozen=True)
class ClassificationResult:
    model: str
    policy_version: str
    input_sha256: str
    probabilities: Mapping[str, float | None]
    # AI-generated, never approvals: short neutral summary, the concrete ask,
    # lowercase addresses asked to act, and any stated deadline.
    summary: str | None = None
    action_text: str | None = None
    action_for: tuple[str, ...] = ()
    due_text: str | None = None
    risk_text: str | None = None
    # Names written in the email (never inferred), canonical when a known name
    # matches (KnownName registry, ``ci names``).
    properties: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    # What the email says about properties, organizations, people and terms
    # (ci_processing.knowledge); None from a run before knowledge extraction.
    knowledge: Knowledge | None = None

    def decisions(self) -> dict[str, Decision]:
        """Every label gets a decision; a label the backend did not answer is unresolved."""
        return {label.name: label.decide(self.probabilities.get(label.name)) for label in LABELS}

    def yes_labels(self) -> set[str]:
        return {name for name, d in self.decisions().items() if d is Decision.YES}


MAX_PROPERTIES = 5
MAX_ENTITIES = 8
MAX_NAME_CHARS = 120


@dataclass(frozen=True)
class KnownName:
    """An administrator-maintained canonical property/project or legal-entity name."""

    kind: str  # 'property' | 'entity'
    canonical: str
    aliases: tuple[str, ...] = ()


def clean_names(values: Iterable[object] | None, limit: int) -> tuple[str, ...]:
    """Strip, collapse spaces, cap length and drop case-insensitive duplicates."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if not isinstance(value, str):
            continue
        name = re.sub(r"\s+", " ", value).strip()[:MAX_NAME_CHARS].strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
        if len(out) >= limit:
            break
    return tuple(out)


def normalize_names(
    names: Iterable[str], kind: str, known: Sequence[KnownName], limit: int
) -> tuple[str, ...]:
    """Map names to their canonical form by case-insensitive canonical/alias match.

    Names with no match are kept as written."""
    lookup: dict[str, str] = {}
    for k in known:  # the administrator's registry only
        if k.kind == kind:
            for alias in (k.canonical, *k.aliases):
                lookup.setdefault(alias.casefold().strip(), k.canonical)
    return clean_names((lookup.get(n.casefold().strip(), n) for n in names), limit)


def with_known_names(
    result: ClassificationResult, known: Sequence[KnownName]
) -> ClassificationResult:
    """The result with its names cleaned and mapped to canonical names."""
    return replace(
        result,
        properties=normalize_names(result.properties, "property", known, MAX_PROPERTIES),
        entities=normalize_names(result.entities, "entity", known, MAX_ENTITIES),
    )


class Classifier(Protocol):
    def classify(
        self, item: ClassificationInput, known: Sequence[KnownName] = ()
    ) -> ClassificationResult: ...


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
    ("cat_leasing_operations", r"\blease\b|\btenant|occupancy|property management", 0.85),
    ("cat_compliance_legal", r"lien|attorney|legal|insurance|dispute|compliance", 0.85),
    ("cat_capital_finance", r"lender|\bloan\b|draw request|investor|refinanc", 0.85),
    ("cat_vendor_performance", r"defects?|delays?|missed|complaint", 0.85),
    ("project_risk", r"defects?|delay|\blien\b|failed inspection|over budget|dispute|claim", 0.9),
    ("cat_general_governance", r"committee|policy|board approval", 0.85),
)
_COMPILED = tuple((name, re.compile(p, re.IGNORECASE), prob) for name, p, prob in _FAKE_RULES)
_FAKE_PROPERTY = re.compile(
    r"\bExample (?:Ridge|Commons)\b(?: Lot \d+)?|\bLot \d+\b|\bBuilding [A-Z]\b|\bSuite \d+\b"
)
_FAKE_ENTITY = re.compile(
    r"\b(?:[A-Z][\w&'-]* +){0,4}[A-Z][\w&'-]*,? +"
    r"(?:L\.?L\.?C\.?|L\.?P\.?|LLP|Inc\.?|Corp\.?|Corporation|Company|Trust|Holdings|Partners|Fund)"
    r"(?![\w])"
)
_FAKE_NO = 0.03


class FakeClassifier:
    """Deterministic keyword classifier (``CLASSIFIER_BACKEND=fake``)."""

    model = "fake-keyword-rules-v1"

    def classify(
        self, item: ClassificationInput, known: Sequence[KnownName] = ()
    ) -> ClassificationResult:
        text = item.full_text()
        probabilities: dict[str, float | None] = {label.name: _FAKE_NO for label in LABELS}
        for name, pattern, prob in _COMPILED:
            if pattern.search(text):
                probabilities[name] = prob
        asks = (probabilities["action_required"] or 0) >= 0.8 or (
            probabilities["approval_required"] or 0
        ) >= 0.8
        return ClassificationResult(
            self.model,
            POLICY_VERSION,
            item.sha256(),
            probabilities,
            summary=f"(fake) {item.subject}",
            action_text=f"(fake) respond to: {item.subject}" if asks else None,
            action_for=item.to if asks else (),
            risk_text=f"(fake) risk in: {item.subject}"
            if (probabilities["project_risk"] or 0) >= 0.75
            else None,
            properties=clean_names(_FAKE_PROPERTY.findall(text), MAX_PROPERTIES),
            entities=clean_names((m.group(0) for m in _FAKE_ENTITY.finditer(text)), MAX_ENTITIES),
            knowledge=validate(_fake_knowledge(item, text), item.source(), item.learned),
        )


def _fake_knowledge(item: ClassificationInput, text: str) -> dict[str, object]:
    raw = fake_extract(text, item.sender.lower())
    listed = {n.name.casefold(): n.name for n in item.learned}
    for name in raw["names"]:
        name["known"] = listed.get(name["written"].casefold())
    return raw
