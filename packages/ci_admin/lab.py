"""Classification lab: run screening, the classifier and the routing rules on a
sample without storing anything.

Two inputs: text pasted into the console (use synthetic or your own mail; it is
sent to the in-tenant Azure AI services, never stored or logged), or a message
already published and visible to the caller, re-run against the current
classifier and compared with what was stored. Only the input hash is audited.

Routing mirrors ``ci_retrieval.search.list_actionable``: an email is an action
item for a person when ``action_required`` or ``approval_required`` is yes and
the classifier named one of the person's addresses in ``action_for``, or named
nobody and the message arrived in one of the person's mailboxes. A
``project_risk`` yes makes it a risk item for everyone with access.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ci_processing.classification import (
    LABELS,
    ClassificationInput,
    ClassificationResult,
    Classifier,
    Decision,
)
from ci_processing.policy import Disposition, Outcome, classification_outcome, screening_gate
from ci_processing.screening import Screener
from ci_retrieval.search import ACTION_LABELS, RISK_LABELS

# Same caps the processing worker uses (ci_email.process), so a re-run of a
# stored message sends the classifier the same input.
CLASSIFY_BODY_CHARS = 12_000
CLASSIFY_ATTACHMENT_CHARS = 3_000
CLASSIFY_MAX_ATTACHMENTS = 5
MAX_LAB_CHARS = 60_000


@dataclass(frozen=True)
class LabBackends:
    screener: Screener
    classifier: Classifier


def addresses(value: str | Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    return sorted({a.strip().lower() for a in items if a and a.strip()})


def routing(
    result: ClassificationResult | None,
    outcome: Outcome,
    person: Sequence[str],
    *,
    received_by: Sequence[str] = (),
    direction: str = "in",
) -> dict[str, Any]:
    """Where this message would surface, with the reason in plain words."""
    searchable = outcome.disposition in (Disposition.INDEX, Disposition.REVIEW)
    out: dict[str, Any] = {
        "disposition": outcome.disposition.value,
        "reason_codes": list(outcome.reason_codes),
        "searchable": searchable,
        "action_item": False,
        "risk_item": False,
        "why": [],
    }
    why: list[str] = out["why"]
    if not searchable:
        why.append(
            {
                Disposition.RESTRICTED: "Restricted: never embedded or returned to anyone.",
                Disposition.HELD: "Held: not published until screening completes.",
                Disposition.NOT_INDEXED: "Bulk mail (spam or newsletter): recorded, not indexed.",
            }.get(outcome.disposition, "Not published.")
        )
        return out
    if outcome.disposition is Disposition.REVIEW:
        why.append("Indexed but flagged for review (" + ", ".join(outcome.reason_codes) + ").")
    assert result is not None  # noqa: S101 - searchable implies it was classified
    yes = result.yes_labels()
    asks = sorted(yes & set(ACTION_LABELS))
    if not asks:
        why.append("Not an action item: neither action_required nor approval_required is yes.")
    elif not person:
        out["action_item"] = True
        why.append(f"Action item ({', '.join(asks)}); no person given to route it to.")
    elif set(result.action_for) & set(person):
        out["action_item"] = True
        why.append("Action item for the person: the classifier named them in action_for.")
    elif result.action_for:
        why.append(
            "Action item, but for someone else: action_for names "
            + ", ".join(result.action_for)
            + "."
        )
    elif direction == "in" and set(received_by) & set(person):
        out["action_item"] = True
        why.append("Action item for the person: nobody was named and it arrived in their mailbox.")
    else:
        why.append("Action item, but nobody was named and it did not arrive in their mailbox.")
    if yes & set(RISK_LABELS):
        out["risk_item"] = True
        why.append("Project risk: listed under risks for everyone with access.")
    return out


def _labels(result: ClassificationResult) -> list[dict[str, Any]]:
    decisions = result.decisions()
    return [
        {
            "label": lb.name,
            "probability": result.probabilities.get(lb.name),
            "decision": decisions[lb.name].value,
            "no_at_or_below": lb.no_at_or_below,
            "yes_at_or_above": lb.yes_at_or_above,
            "question": lb.question,
        }
        for lb in LABELS
    ]


def run(
    backends: LabBackends,
    item: ClassificationInput,
    person: Sequence[str],
    *,
    received_by: Sequence[str] = (),
    direction: str = "in",
) -> dict[str, Any]:
    """Screen, then classify (only if screening allows, as in processing), then route."""
    screen_text = "\n\n".join(
        [item.subject, item.body_text, *(text for _, text in item.attachments)]
    )
    screening = backends.screener.screen(screen_text)
    checks = [
        {"check": c.check_name, "decision": c.decision.value, "reason_code": c.reason_code}
        for c in screening.checks
    ]
    gate = screening_gate(screening)
    result = None if gate is not None else backends.classifier.classify(item)
    outcome = gate or classification_outcome(result)  # type: ignore[arg-type]
    return {
        "input_sha256": item.sha256(),
        "screening": checks,
        "classified": result is not None,
        "model": result.model if result else None,
        "policy_version": result.policy_version if result else None,
        "labels": _labels(result) if result else [],
        "yes_labels": sorted(result.yes_labels()) if result else [],
        "uncertain_labels": sorted(
            n for n, d in result.decisions().items() if d in (Decision.REVIEW, Decision.UNRESOLVED)
        )
        if result
        else [],
        "summary_ai": result.summary if result else None,
        "action_ai": result.action_text if result else None,
        "action_for_ai": list(result.action_for) if result else [],
        "due_ai": result.due_text if result else None,
        "risk_ai": result.risk_text if result else None,
        "routing": routing(result, outcome, person, received_by=received_by, direction=direction),
    }


def pasted_input(body: dict[str, Any]) -> ClassificationInput:
    subject = str(body.get("subject") or "")[:1000]
    text = str(body.get("body") or "")
    if not (subject.strip() or text.strip()):
        raise ValueError("enter a subject or a body")
    if len(text) > MAX_LAB_CHARS:
        raise ValueError(f"the body is limited to {MAX_LAB_CHARS} characters")
    return ClassificationInput(
        subject=subject,
        body_text=text[:CLASSIFY_BODY_CHARS],
        sender=(addresses(body.get("sender")) or [""])[0],
        to=tuple(addresses(body.get("to"))),
        cc=tuple(addresses(body.get("cc"))),
        sent=str(body.get("sent") or ""),
    )


def stored_input(email: dict[str, Any]) -> ClassificationInput:
    """Rebuild the classifier input from an authorized ``get_email`` result."""
    people: dict[str, list[str]] = {}
    for p in email["participants"]:
        people.setdefault(p["role"], []).append(p["email_normalized"])
    body = next((t["text"] for t in email["text"] if t["source"] == "body"), "")
    attachments = [t for t in email["text"] if t["source"] == "attachment"]
    return ClassificationInput(
        subject=email["subject"] or "",
        body_text=body[:CLASSIFY_BODY_CHARS],
        sender=(people.get("from") or [""])[0],
        to=tuple(sorted(people.get("to", []))),
        cc=tuple(sorted(people.get("cc", []))),
        sent=email["received_at"],
        attachments=tuple(
            (a["attachment_name"] or "", a["text"][:CLASSIFY_ATTACHMENT_CHARS])
            for a in attachments[:CLASSIFY_MAX_ATTACHMENTS]
        ),
    )
