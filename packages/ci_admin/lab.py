"""Classification lab: run the bulk pre-filter, screening, the classifier and the
routing rules on a sample without storing anything.

Two inputs: text pasted into the console (use synthetic or your own mail; it is
sent to the in-tenant Azure AI services, never stored or logged), or a message
already published and visible to the caller, re-run against the current
classifier and compared with what was stored. Only the input hash is audited.

Routing mirrors ``ci_retrieval.search.list_actionable``: an email is an action
item for a person when ``action_required`` or ``approval_required`` is yes, it
is not possible bulk mail, not from an automated sender and not sent by the
person, and the classifier named one of the person's addresses in
``action_for`` (or named nobody and it was sent to the person alone). A
``project_risk`` yes makes it a risk item for everyone with access.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from ci_core.ops import Schedule
from ci_core.settings import SensitiveDataPolicy, Settings
from ci_processing.classification import (
    LABELS,
    ClassificationInput,
    ClassificationResult,
    Classifier,
    Decision,
    KnownName,
    with_known_names,
)
from ci_processing.policy import Disposition, Outcome, classification_outcome, screening_gate
from ci_processing.prefilter import REASONS as PREFILTER_REASONS
from ci_processing.prefilter import bulk_skip_reason
from ci_processing.redaction import Redaction, RedactionUnavailable, Redactor
from ci_processing.routing import PersonProfile, PersonRouter, rule_out
from ci_processing.screening import DISABLED, DisabledScreener, Screener
from ci_processing.usage import Meter
from ci_retrieval.search import ACTION_LABELS, AUTOMATED_SENDER, BULK_LABELS, RISK_LABELS

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
    redactor: Redactor | None = None  # None: SENSITIVE_DATA_POLICY=restrict
    router: PersonRouter | None = None  # step 2 of action items, for people with a profile
    prefilter: bool = False  # BULK_PREFILTER_ENABLED, as in processing
    prefilter_allow_domains: frozenset[str] = frozenset()
    masking_disabled: bool = False  # switched off in the console (redactor is then None)
    meter: Meter | None = None  # paid calls, recorded on the cost page (source 'lab')


def with_switches(backends: LabBackends, settings: Settings, schedule: Schedule) -> LabBackends:
    """The console's switches for the paid checks, as processing applies them."""
    if settings.sensitive_data_policy is not SensitiveDataPolicy.MASK:
        return backends
    if not schedule.prompt_shield_enabled:
        backends = replace(backends, screener=DisabledScreener())
    if not schedule.masking_enabled:
        backends = replace(backends, redactor=None, masking_disabled=True)
    return backends


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
    sender: str = "",
    to: Sequence[str] = (),
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
    if not searchable and set(outcome.reason_codes) & set(PREFILTER_REASONS):
        why.append(
            "Skipped by the bulk pre-filter ("
            + ", ".join(outcome.reason_codes)
            + "): recorded, not indexed, and no AI service was called."
        )
        return out
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
    decisions = result.decisions()
    asks = sorted(yes & set(ACTION_LABELS))
    bulkish = [n for n in BULK_LABELS if decisions[n] is not Decision.NO]
    sender = sender.lower()
    if not asks:
        why.append("Not an action item: neither action_required nor approval_required is yes.")
    elif bulkish:
        why.append(f"Not an action item: may be bulk mail ({', '.join(bulkish)} not ruled out).")
    elif re.match(AUTOMATED_SENDER, sender):
        why.append("Not an action item: sent by an automated address.")
    elif not person:
        out["action_item"] = True
        why.append(f"Action item ({', '.join(asks)}); no person given to route it to.")
    elif sender in person:
        why.append("Not an action item for the person: they sent it.")
    elif set(result.action_for) & set(person):
        out["action_item"] = True
        why.append("Action item for the person: the classifier named them in action_for.")
    elif result.action_for:
        why.append(
            "Action item, but for someone else: action_for names "
            + ", ".join(result.action_for)
            + "."
        )
    elif direction == "in" and set(received_by) & set(person) and to and set(to) <= set(person):
        out["action_item"] = True
        why.append("Action item for the person: nobody was named and it was sent to them alone.")
    else:
        why.append(
            "Not an action item for the person: nobody was named and it was not sent to them alone."
        )
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
    profile: PersonProfile | None = None,
    known: Sequence[KnownName] = (),
    full_body: str | None = None,
    has_real_attachment: bool | None = None,
) -> dict[str, Any]:
    """Pre-filter, screen, then classify (only if both allow, as in processing), then route.

    ``full_body`` is the uncut body for the pre-filter (processing checks up to
    30,000 characters; the classifier sees 12,000). With a ``profile`` (step 2
    of action items), the person check decides whether it is that person's
    action item, exactly as processing would.
    """
    skipped = (
        bulk_skip_reason(
            sender=item.sender,
            subject=item.subject,
            body=item.body_text if full_body is None else full_body,
            has_real_attachment=bool(item.attachments)
            if has_real_attachment is None
            else has_real_attachment,
            allowed_domains=backends.prefilter_allow_domains,
        )
        if backends.prefilter
        else None
    )
    checks: list[dict[str, Any]] = []
    gate: Outcome | None = None
    masked: dict[str, int] = {}
    if skipped is not None:
        gate = Outcome(Disposition.NOT_INDEXED, (skipped,))
    else:
        # The text the classifier reads, which is what processing screens when
        # masking is on (ci_email.process._screen_text).
        screening = backends.screener.screen(
            "\n\n".join([item.subject, item.body_text, *(text for _, text in item.attachments)])
        )
        checks = [
            {
                "check": c.check_name,
                "decision": "off" if c.service_ref == DISABLED else c.decision.value,
                "reason_code": c.reason_code,
            }
            for c in screening.checks
        ]
        gate = screening_gate(screening)
    if gate is None and backends.redactor is not None:
        try:
            item, masked = _masked(backends.redactor, item)
        except RedactionUnavailable:
            gate = Outcome(Disposition.HELD, ("redaction_pending",))
    result = (
        None
        if gate is not None
        else with_known_names(backends.classifier.classify(item, known), known)
    )
    if gate is not None:
        outcome = gate
    else:
        assert result is not None  # noqa: S101 - classified when screening allowed
        outcome = classification_outcome(result, mask_sensitive=backends.redactor is not None)
    route = routing(
        result,
        outcome,
        person,
        received_by=received_by,
        direction=direction,
        sender=item.sender,
        to=addresses(item.to),
    )
    person_check = None
    if profile is not None and result is not None and route["searchable"]:
        person_check = _person_check(backends, result, item, profile, received_by)
        route["action_item"] = person_check["decision"] == "yes"
        route["why"] = [w for w in route["why"] if "action item" not in w.lower()]
        route["why"].insert(0, person_check["why"])
    if backends.masking_disabled and skipped is None:
        route["why"].insert(
            0,
            "Masking is switched off: nothing was masked, and mail the classifier marks"
            " as holding ID, bank or card numbers or credentials is restricted instead.",
        )
    if masked:
        route["why"].insert(
            0,
            "Masked before classification and indexing: "
            + ", ".join(f"{k} x{n}" for k, n in sorted(masked.items()))
            + ".",
        )
    return {
        "input_sha256": item.sha256(),
        "prefilter": skipped,
        "screening": checks,
        "masked": masked,
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
        "properties_ai": list(result.properties) if result else [],
        "entities_ai": list(result.entities) if result else [],
        "routing": route,
        "person_check": person_check,
    }


def _person_check(
    backends: LabBackends,
    result: ClassificationResult,
    item: ClassificationInput,
    profile: PersonProfile,
    received_by: Sequence[str],
) -> dict[str, Any]:
    mailbox = next((m for m in received_by if m in {a.lower() for a in profile.addresses}), "")
    ruled_out = rule_out(result, item, profile, mailbox)
    if ruled_out or backends.router is None:
        reason = ruled_out or "no_router"
        return {
            "person": profile.display_name,
            "decision": "no",
            "probability": None,
            "action": None,
            "reason": reason,
            "by": "rules",
            "why": f"Step 2 for {profile.display_name}: not theirs by the rules ({reason}).",
        }
    route = backends.router.route(item, profile)
    shown = "?" if route.probability is None else f"{route.probability:.2f}"
    return {
        "person": profile.display_name,
        "decision": route.decision.value,
        "probability": route.probability,
        "action": route.action,
        "reason": route.reason,
        "by": route.model,
        "why": f"Step 2 for {profile.display_name}: {route.decision.value} ({shown},"
        f" {route.reason or 'no reason'}).",
    }


def _masked(
    redactor: Redactor, item: ClassificationInput
) -> tuple[ClassificationInput, dict[str, int]]:
    """The same masking processing applies, before classification."""
    result: Redaction = redactor.redact(
        [item.subject, item.body_text, *(text for _, text in item.attachments)]
    )
    subject, body, *texts = result.texts
    attachments = tuple(
        (name, text) for (name, _), text in zip(item.attachments, texts, strict=True)
    )
    return replace(item, subject=subject, body_text=body, attachments=attachments), result.counts


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
