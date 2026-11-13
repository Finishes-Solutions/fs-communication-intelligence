"""Risk-only access (Matthew, 2026-11-13): an agent may surface project-risk emails
without returning email links, with the detail chosen per reader.

A caller whose only reading role is ``Communications.Reader.Risks``
(``Principal.risk_only``):

- reaches only published project-risk emails, from every enrolled mailbox. The
  SQL authorization (``ci_retrieval.search._AUTH``) enforces it for every query,
  so another tool or page cannot widen it;
- may use only ``risk_overview`` and the risk part of ``list_actionable``; every
  other MCP tool refuses (``REFUSAL``);
- gets each risk email cut down to the fields chosen for them in the console's
  People page (migration 0029, ``ci_core.operator.set_risk_fields``). No choice
  means counts and charts only;
- never gets ``view_url``, ``download_url``, ``web_link``, ``communication_id``,
  mailbox addresses or email text, whatever was chosen.

Per-reader limits (migration 0024) still apply on top.
"""

from __future__ import annotations

from typing import Any

from ci_core.db import Connection
from ci_core.operator import RISK_FIELDS
from ci_retrieval.identity import Principal

REFUSAL = {
    "error": "not_available_for_risk_only_access",
    "note": "This account has risk-only access: use risk_overview or list_actionable for"
    " project risks. Emails, attachments, threads, links and the knowledge base are not"
    " available to it.",
}

RISK_ONLY_NOTE = (
    "Risk-only access: project-risk emails only, without email links or email text, and"
    " only the fields chosen for this account. Ratings and summaries are AI proposals from"
    " the email text alone, uncalibrated. An empty result means no risk found in"
    " authorized, indexed mail."
)

# The aggregate parts of risk_overview a risk-only reader always gets: counts by
# period, category and property, the matrix and the heatmap. Per-company counts
# need sender_domain; per-mailbox counts are never shown (they name people).
_AGGREGATES = (
    "days",
    "since",
    "total",
    "previous_total",
    "series",
    "by_category",
    "by_property",
    "co_signals",
    "matrix",
    "category_series",
    "heatmap",
    "top_properties",
)


def fields_for(conn: Connection, principal: Principal) -> frozenset[str]:
    """The fields this reader is shown; empty (counts only) without a choice, an
    address, or migration 0029."""
    if not principal.upn:
        return frozenset()
    row = conn.execute(
        "SELECT CASE WHEN to_regprocedure('ci.risk_fields(text)') IS NULL THEN NULL"
        " ELSE ci.risk_fields(%s) END AS fields",
        (principal.upn,),
    ).fetchone()
    chosen = (row or {}).get("fields") or []
    return frozenset(f for f in chosen if f in RISK_FIELDS)


def _domain(sender: str | None) -> str | None:
    return sender.split("@", 1)[1].lower() if sender and "@" in sender else None


def item(
    row: dict[str, Any], fields: frozenset[str], documents: list[dict[str, Any]]
) -> dict[str, Any]:
    """One risk email with only the chosen fields (a risk_overview item)."""
    out: dict[str, Any] = {}
    if "date" in fields:
        out["received_at"] = row.get("received_at")
    if "subject" in fields:
        out["subject"] = row.get("subject")
    if "sender" in fields:
        out["sender"] = row.get("sender")
    if "sender_domain" in fields:
        out["sender_domain"] = _domain(row.get("sender"))
    if "property" in fields:
        out["properties_ai"] = list(row.get("properties_ai") or [])
    if "category" in fields:
        out["categories"] = [x for x in row.get("labels") or [] if x.startswith("cat_")]
    if "summary" in fields:
        out["risk_ai"] = row.get("risk_ai")
    if "rating" in fields:
        out["risk_rating_ai"] = row.get("risk_rating_ai")
    if "documents" in fields:
        out["documents"] = documents
    return out


def documents_by_email(
    conn: Connection, principal: Principal, communication_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Attachment names and types of these emails, through the same authorized
    lookup as file links, without ids, file ids or links."""
    from ci_retrieval import search as retrieval

    found: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(communication_ids), retrieval.MAX_LINK_EMAILS):
        batch = communication_ids[start : start + retrieval.MAX_LINK_EMAILS]
        listed = retrieval.file_links(conn, principal, batch, action="risk_documents")
        for e in listed["emails"]:
            found[e["communication_id"]] = [
                {"name": f.get("name"), "content_type": f.get("content_type")} for f in e["files"]
            ]
    return found


def overview(
    conn: Connection,
    principal: Principal,
    found: dict[str, Any],
    fields: frozenset[str],
    item_limit: int,
) -> dict[str, Any]:
    """risk_overview for a risk-only reader: the aggregates, plus the newest risk
    emails with the chosen fields only when any per-email field was chosen."""
    out = {k: found[k] for k in _AGGREGATES if k in found}
    if "sender_domain" in fields:
        out["by_company"] = found.get("by_company", [])
    out["fields_shown"] = [f for f in RISK_FIELDS if f in fields]
    out["note"] = RISK_ONLY_NOTE
    if not fields:
        out["items_note"] = "Counts only: no per-email details are shown to this account."
        return out
    rows = found.get("items", [])[: max(0, min(item_limit, 200))]
    docs = (
        documents_by_email(conn, principal, [r["communication_id"] for r in rows])
        if "documents" in fields and rows
        else {}
    )
    out["items"] = [item(r, fields, docs.get(r["communication_id"], [])) for r in rows]
    out["items_total"] = len(found.get("items", []))
    return out
