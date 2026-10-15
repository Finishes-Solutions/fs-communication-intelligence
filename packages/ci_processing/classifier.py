"""Classifier interface (ADR-017) and the local fake backend.

Backends: ``azure_openai`` (cloud default, not built yet), ``decisions_api``
(only after data-processing approval, D-C1; not built yet) and ``fake``.

The fake is a keyword rule set over synthetic fixtures. Its probabilities are
made-up constants chosen to exercise every outcome path; they say nothing
about how a real model would score real mail.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from ci_core.settings import ClassifierBackend
from ci_processing.labels import LABELS


@dataclass(frozen=True)
class ClassifierInput:
    """Treated as untrusted text: instructions inside it are never followed."""

    subject: str
    body: str
    from_address: str | None
    direction: str

    def sha256(self) -> str:
        data = json.dumps([self.subject, self.body, self.from_address, self.direction])
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class ClassifierOutput:
    backend: ClassifierBackend
    model: str
    probabilities: dict[str, float | None]  # None = no answer (unresolved)


class Classifier(Protocol):
    backend: ClassifierBackend
    model: str

    def classify(self, item: ClassifierInput) -> ClassifierOutput: ...


STRONG, WEAK, BASE = 0.96, 0.50, 0.03

# label -> (strong patterns, weak patterns); case-insensitive regexes
_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "spam": ((r"you'?ve been selected", r"exclusive prize", r"claim your (reward|prize)"), ()),
    "phishing": (
        (r"updated (remittance|bank)", r"new bank(ing)? details", r"change of bank"),
        (r"verify your account",),
    ),
    "pii": ((r"\(\d{3}\) ?\d{3}-\d{4}", r"\b\d{3}[.-]\d{3}[.-]\d{4}\b", r"\bm(obile)?: "), ()),
    "sensitive_pii": (
        (r"\b\d{3}-\d{2}-\d{4}\b", r"social security", r"routing number"),
        (r"date of birth",),
    ),
    "potential_phi": (
        (r"diagnos", r"medical record", r"\bpatient\b"),
        (r"medical leave", r"surgery", r"doctor"),
    ),
    "credentials": ((r"api[_ ]?key\s*[:=]", r"password\s*[:=]", r"\bsk_(live|test)_\w+"), ()),
    "confidential_business": (
        (r"confidential", r"\bloan\b", r"\bdraw\b", r"term sheet", r"purchase price"),
        (),
    ),
    "project_related": (
        (
            r"example ridge",
            r"building [a-z]\b",
            r"lot \d+",
            r"phase \d",
            r"\bsuite \d+",
            r"example commons",
            r"westpark",
        ),
        (),
    ),
    "invoice_receipt": ((r"\binvoice\b", r"pay app", r"remittance", r"\breceipt\b"), ()),
    "approval_required": ((r"\bapprov", r"sign[- ]off", r"please confirm"), ()),
    "action_required": (
        (
            r"please (send|review|confirm|approve|sign|provide)",
            r"needed by",
            r"by (monday|tuesday|wednesday|thursday|friday)",
            r"can you",
            r"need you to",
        ),
        (r"\bplease\b",),
    ),
    "urgent": ((r"\burgent\b", r"\btoday\b", r"\basap\b", r"end of day"), (r"tomorrow",)),
    "newsletter": ((r"unsubscribe", r"\bdigest\b", r"\bedition\b", r"newsletter"), ()),
    "cat_development_construction": (
        (
            r"change order",
            r"framing",
            r"inspection",
            r"drainage",
            r"grading",
            r"permit",
            r"drawings",
            r"site walk",
            r"superintendent",
            r"schedule",
        ),
        (),
    ),
    "cat_payment_billing": (
        (r"\binvoice\b", r"remittance", r"\bpayment\b", r"pay app", r"\bdraw\b"),
        (),
    ),
    "cat_leasing_operations": ((r"\blease\b", r"\btenant\b", r"renewal", r"\btour\b"), ()),
    "cat_compliance_legal": (
        (r"notice of claim", r"\blegal\b", r"attorney", r"\bllp\b", r"insurance", r"liability"),
        (),
    ),
    "cat_capital_finance": (
        (r"\bloan\b", r"\blender\b", r"\bdraw\b", r"refinanc", r"investor"),
        (),
    ),
    "cat_vendor_performance": ((r"missed", r"\bdelay", r"defect", r"complaint"), ()),
    "cat_general_governance": (
        (r"\bpolicy\b", r"committee", r"\bboard\b", r"lunch (&|and) learn"),
        (),
    ),
}


class FakeClassifier:
    backend = ClassifierBackend.FAKE
    model = "fake-keyword-rules.v1"

    def classify(self, item: ClassifierInput) -> ClassifierOutput:
        text = f"{item.subject}\n{item.body}".lower()
        probs: dict[str, float | None] = {}
        for spec in LABELS:
            strong, weak = _RULES.get(spec.name, ((), ()))
            if any(re.search(p, text) for p in strong):
                probs[spec.name] = STRONG
            elif any(re.search(p, text) for p in weak):
                probs[spec.name] = WEAK
            else:
                probs[spec.name] = BASE
        return ClassifierOutput(self.backend, self.model, probs)


def make_classifier(backend: ClassifierBackend) -> Classifier:
    if backend is ClassifierBackend.FAKE:
        return FakeClassifier()
    raise NotImplementedError(
        f"CLASSIFIER_BACKEND={backend.value} is not built yet (PR 6); use fake locally"
    )
