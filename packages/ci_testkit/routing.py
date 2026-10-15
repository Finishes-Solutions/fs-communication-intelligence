"""Preview of Exhibit A routing: which department intake a message WOULD go to.

Display only. Nothing is written to the outbox and nothing is sent: monday.com
routing is out of scope for this build (CLAUDE.md section 9). The department
names are placeholders until the intake boards are defined.
"""

from __future__ import annotations

from dataclasses import dataclass

DEPARTMENTS: dict[str, str] = {
    "cat_development_construction": "Development & Construction intake",
    "cat_payment_billing": "Accounting / AP intake",
    "cat_leasing_operations": "Leasing & Property Operations intake",
    "cat_compliance_legal": "Legal & Compliance intake",
    "cat_capital_finance": "Capital & Finance intake",
    "cat_vendor_performance": "Procurement & Vendor Management intake",
    "cat_general_governance": "Executive & Governance intake",
}
ACTIONABLE = ("approval_required", "action_required", "invoice_receipt")


@dataclass(frozen=True)
class RoutingPreview:
    destinations: tuple[str, ...]
    triggers: tuple[str, ...]
    status: str  # plain-English summary for the UI


def preview(outcome: str | None, decisions: dict[str, str]) -> RoutingPreview:
    if outcome not in ("index", "review"):
        return RoutingPreview((), (), "Not routed: the item is not indexed.")
    triggers = tuple(a for a in ACTIONABLE if decisions.get(a) == "yes")
    destinations = tuple(
        name for label, name in DEPARTMENTS.items() if decisions.get(label) == "yes"
    )
    if not triggers:
        return RoutingPreview(destinations, (), "Informational: searchable, no intake item.")
    if not destinations:
        return RoutingPreview((), triggers, "Actionable, but no department category was confident.")
    if outcome == "review":
        return RoutingPreview(
            destinations, triggers, "Would wait for human review before any intake item is created."
        )
    return RoutingPreview(
        destinations, triggers, "Would create a department intake item for approval (not sent)."
    )
