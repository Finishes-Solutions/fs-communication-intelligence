"""The provisional label set and outcome rules (CLAUDE.md section 7).

Uncalibrated: thresholds must be calibrated on a labelled pilot sample before
anything depends on them. A probability <= no means "no", >= yes means "yes",
anything between means "review", and a missing answer is "unresolved" (never 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LabelDecision = Literal["yes", "no", "review", "unresolved"]
Outcome = Literal["index", "review", "restricted", "not_indexed"]

POLICY_VERSION = "labels-2026-10-14.uncalibrated"


@dataclass(frozen=True)
class LabelSpec:
    name: str
    no: float
    yes: float
    question: str


LABELS: tuple[LabelSpec, ...] = (
    LabelSpec("spam", 0.15, 0.95, "Unsolicited bulk junk or irrelevant mass promotion?"),
    LabelSpec(
        "phishing", 0.10, 0.80, "Phishing, impersonation, scam or payment-redirection indicators?"
    ),
    LabelSpec(
        "pii", 0.20, 0.80, "Identifies a person, including routine business contact details?"
    ),
    LabelSpec(
        "sensitive_pii", 0.10, 0.75, "SSNs, government IDs, bank/card numbers, payroll or similar?"
    ),
    LabelSpec("potential_phi", 0.10, 0.75, "Identifiable health information?"),
    LabelSpec(
        "credentials", 0.10, 0.75, "Actual secrets, passwords, codes, API keys or credential links?"
    ),
    LabelSpec("confidential_business", 0.15, 0.80, "Substantive nonpublic business information?"),
    LabelSpec(
        "project_related", 0.20, 0.80, "About a specific project, property, asset or opportunity?"
    ),
    LabelSpec(
        "invoice_receipt", 0.20, 0.80, "A specific invoice, receipt, pay application or payment?"
    ),
    LabelSpec(
        "approval_required", 0.20, 0.80, "Requests a pending decision from the mailbox owner?"
    ),
    LabelSpec(
        "action_required", 0.20, 0.80, "Asks the mailbox owner for a concrete pending action?"
    ),
    LabelSpec("urgent", 0.15, 0.85, "Credible unresolved need due today or next business day?"),
    LabelSpec("newsletter", 0.20, 0.85, "Newsletter, digest or informational mass mailing?"),
    LabelSpec(
        "cat_development_construction",
        0.20,
        0.70,
        "Design, permits, inspections, contractors, schedule, scope, change orders?",
    ),
    LabelSpec("cat_payment_billing", 0.20, 0.70, "Invoices, pay apps, draws, payment status?"),
    LabelSpec(
        "cat_leasing_operations",
        0.20,
        0.70,
        "Leasing, occupancy, tenants, property management/operations?",
    ),
    LabelSpec(
        "cat_compliance_legal", 0.20, 0.70, "Compliance, legal, insurance, regulatory, disputes?"
    ),
    LabelSpec(
        "cat_capital_finance",
        0.20,
        0.70,
        "Capital, lenders, loans, investors, refinancing, acquisitions?",
    ),
    LabelSpec("cat_vendor_performance", 0.20, 0.70, "Vendor quality, delays, defects, complaints?"),
    LabelSpec(
        "cat_general_governance", 0.20, 0.70, "Internal approvals, policies, committee decisions?"
    ),
)
LABELS_BY_NAME = {spec.name: spec for spec in LABELS}

RESTRICTING = ("credentials", "potential_phi", "sensitive_pii")


def decide(spec: LabelSpec, probability: float | None) -> LabelDecision:
    if probability is None:
        return "unresolved"
    if probability <= spec.no:
        return "no"
    if probability >= spec.yes:
        return "yes"
    return "review"


def derive_outcome(decisions: dict[str, LabelDecision]) -> tuple[Outcome, list[str]]:
    """Apply the section 7 rules, most protective first.

    1. credentials / potential_phi / sensitive_pii = yes -> restricted (never embedded or returned)
    2. spam or newsletter = yes -> not_indexed (recorded only)
    3. any of the restricting labels uncertain, or phishing != no -> review (indexed and flagged)
    4. otherwise -> index
    """

    def get(name: str) -> LabelDecision:
        return decisions.get(name, "unresolved")

    restricted = [f"restricted_{n}" for n in RESTRICTING if get(n) == "yes"]
    if restricted:
        return "restricted", restricted
    not_indexed = [f"not_indexed_{n}" for n in ("spam", "newsletter") if get(n) == "yes"]
    if not_indexed:
        return "not_indexed", not_indexed
    review = [f"uncertain_{n}" for n in RESTRICTING if get(n) in ("review", "unresolved")]
    if get("phishing") != "no":
        review.append(f"phishing_{get('phishing')}")
    if review:
        return "review", review
    return "index", []
