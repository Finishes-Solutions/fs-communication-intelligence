"""Report builder: pick a dataset, filters, grouping and columns; preview or export.

Mail datasets (messages, action items, project risks) come from the caller's
own grants through ``ci_retrieval.reports`` and carry metadata and AI proposals
only, never email bodies or attachment text (Matthew, 2026-10-28). Operations
datasets (runs, mailboxes, label review results) hold no message content.
Every preview and export is audited with the ids it returned.

Exports are CSV or Excel. Subjects and senders are untrusted email content, so
nothing may run as a spreadsheet formula: CSV values starting with = + - @ or
a tab/carriage return get a leading apostrophe, and Excel cells are written as
explicit strings.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook  # type: ignore[import-untyped]
from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]
from openpyxl.styles import Font  # type: ignore[import-untyped]

from ci_admin import calibration
from ci_core.db import Connection
from ci_core.operator import OperatorError, audit, list_mailboxes
from ci_core.ops import recent_runs
from ci_retrieval import reports as retrieval
from ci_retrieval.identity import Principal
from ci_retrieval.search import Filters

# Friendly label names for people; the stored keys stay the source of truth.
LABEL_TITLES = {
    "spam": "Spam",
    "phishing": "Phishing",
    "pii": "Personal Info",
    "sensitive_pii": "Sensitive Personal Info",
    "potential_phi": "Health Info",
    "credentials": "Credentials",
    "confidential_business": "Confidential",
    "project_related": "Project",
    "invoice_receipt": "Invoice",
    "approval_required": "Needs Approval",
    "action_required": "Needs Action",
    "urgent": "Urgent",
    "newsletter": "Newsletter",
    "project_risk": "Project Risk",
    "cat_development_construction": "Construction",
    "cat_payment_billing": "Payment",
    "cat_leasing_operations": "Leasing",
    "cat_compliance_legal": "Legal & Compliance",
    "cat_capital_finance": "Finance",
    "cat_vendor_performance": "Vendor Issues",
    "cat_general_governance": "Governance",
}

MAIL_DATASETS = retrieval.DATASETS
OPS_DATASETS = ("runs", "mailboxes", "review_metrics")
DATASETS = (*MAIL_DATASETS, *OPS_DATASETS)
PREVIEW_ROWS = 200
FORMATS = ("csv", "xlsx")
_UNSAFE_START = ("=", "+", "-", "@", "\t", "\r")


def label_title(key: str) -> str:
    return LABEL_TITLES.get(key) or key.removeprefix("cat_").replace("_", " ").capitalize()


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    kind: str = "text"  # text | number | datetime | labels | bool | rate


MAIL_COLUMNS = (
    Column("received_at", "Received", "datetime"),
    Column("mailbox", "Mailbox"),
    Column("direction", "Direction"),
    Column("sender", "Sender"),
    Column("sender_domain", "Sender Domain"),
    Column("subject", "Subject"),
    Column("labels", "Labels", "labels"),
    Column("uncertain_labels", "Uncertain Labels", "labels"),
    Column("review_flag", "Flagged for Review", "bool"),
    Column("summary_ai", "AI Summary"),
    Column("action_ai", "AI Action"),
    Column("action_for_ai", "Action For"),
    Column("due_ai", "Due"),
    Column("risk_ai", "AI Project Risk"),
    Column("properties_ai", "Properties / Projects"),
    Column("entities_ai", "Legal Entities"),
    Column("attachments", "Attachments", "number"),
    Column("web_link", "Outlook Link"),
    Column("communication_id", "Message ID"),
)
GROUP_COLUMNS = (
    Column("key", "Group"),
    Column("messages", "Messages", "number"),
    Column("actions", "Asking for Action", "number"),
    Column("risks", "Project Risks", "number"),
    Column("flagged", "Flagged for Review", "number"),
    Column("first_at", "First", "datetime"),
    Column("last_at", "Last", "datetime"),
)
RUN_COLUMNS = (
    Column("started_at", "Started", "datetime"),
    Column("finished_at", "Finished", "datetime"),
    Column("kind", "Kind"),
    Column("trigger", "Trigger"),
    Column("window_hours", "Window (Hours)", "number"),
    Column("status", "Status"),
    Column("error_code", "Error Code"),
    Column("counts", "Counts"),
)
MAILBOX_COLUMNS = (
    Column("address", "Mailbox"),
    Column("recipient_type", "Type"),
    Column("scope_state", "State"),
    Column("test_approved", "Test Approved", "bool"),
    Column("production_approved", "Production Approved", "bool"),
    Column("excluded", "Excluded", "bool"),
)
METRIC_COLUMNS = (
    Column("label", "Label", "labels"),
    Column("judged", "Checked", "number"),
    Column("applies", "Applies", "number"),
    Column("precision", "Precision", "rate"),
    Column("recall", "Recall", "rate"),
    Column("uncertain", "AI Unsure", "number"),
    Column("yes_at_or_above", "Yes Threshold Now", "number"),
    Column("suggested_yes", "Suggested Yes Threshold", "number"),
    Column("needed", "Checks Needed for a Suggestion", "number"),
)
COLUMNS: dict[str, tuple[Column, ...]] = {
    "messages": MAIL_COLUMNS,
    "actions": MAIL_COLUMNS,
    "risks": MAIL_COLUMNS,
    "grouped": GROUP_COLUMNS,
    "runs": RUN_COLUMNS,
    "mailboxes": MAILBOX_COLUMNS,
    "review_metrics": METRIC_COLUMNS,
}
GROUP_TITLES = {
    "mailbox": "Mailbox",
    "sender": "Sender",
    "sender_domain": "Sender Domain",
    "label": "Label",
    "property": "Property / Project",
    "entity": "Legal Entity",
    "day": "Day",
    "week": "Week Of",
    "month": "Month",
}
DEFAULT_MAIL_COLUMNS = (
    "received_at",
    "mailbox",
    "sender",
    "subject",
    "labels",
    "properties_ai",
    "summary_ai",
    "action_ai",
    "due_ai",
    "risk_ai",
)


@dataclass(frozen=True)
class ReportRequest:
    dataset: str
    group_by: str | None
    since: date | None
    until: date | None
    mailbox: str | None
    sender: str | None
    label: str | None
    person: str | None
    columns: tuple[str, ...]
    label_mode: str  # names | keys

    @property
    def kind(self) -> str:
        return "grouped" if self.group_by else self.dataset


def _date(value: Any, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise OperatorError(f"{name} must be a date like 2026-10-28") from exc


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse(body: dict[str, Any]) -> ReportRequest:
    dataset = str(body.get("dataset") or "messages")
    if dataset not in DATASETS:
        raise OperatorError(f"dataset must be one of {', '.join(DATASETS)}")
    group_by = _text(body.get("group_by"))
    if group_by and dataset not in MAIL_DATASETS:
        raise OperatorError("only mail datasets can be grouped")
    if group_by and group_by not in retrieval.GROUPS:
        raise OperatorError(f"group_by must be one of {', '.join(retrieval.GROUPS)}")
    since, until = _date(body.get("since"), "since"), _date(body.get("until"), "until")
    if since and until and until < since:
        raise OperatorError("the end date is before the start date")
    kind = "grouped" if group_by else dataset
    allowed = {c.key for c in COLUMNS[kind]}
    wanted = [str(c) for c in body.get("columns") or [] if str(c) in allowed]
    if not wanted:
        wanted = [c.key for c in COLUMNS[kind]]
        if kind in MAIL_DATASETS:
            wanted = list(DEFAULT_MAIL_COLUMNS)
    label_mode = "keys" if body.get("label_mode") == "keys" else "names"
    return ReportRequest(
        dataset=dataset,
        group_by=group_by,
        since=since,
        until=until,
        mailbox=_text(body.get("mailbox")),
        sender=_text(body.get("sender")),
        label=_text(body.get("label")),
        person=_text(body.get("person")),
        columns=tuple(wanted),
        label_mode=label_mode,
    )


def _bounds(req: ReportRequest, tz: ZoneInfo) -> tuple[datetime | None, datetime | None]:
    since = datetime.combine(req.since, time.min, tz) if req.since else None
    until = datetime.combine(req.until + timedelta(days=1), time.min, tz) if req.until else None
    return since, until


def run(
    req: ReportRequest,
    principal: Principal,
    *,
    search_conn: Callable[[], Connection],
    operator_conn: Callable[[], Connection],
    tz_name: str,
    limit: int,
    action: str,
) -> dict[str, Any]:
    """Rows for the request (up to ``limit``), the total, and the columns used."""
    tz = ZoneInfo(tz_name)
    since, until = _bounds(req, tz)
    rows: list[dict[str, Any]]
    if req.dataset in MAIL_DATASETS:
        filters = Filters(
            mailbox=req.mailbox, sender=req.sender, since=since, until=until, label=req.label
        )
        with search_conn() as conn:
            if req.group_by:
                data = retrieval.report_groups(
                    conn,
                    principal,
                    req.dataset,
                    req.group_by,
                    filters,
                    person=req.person,
                    tz=tz_name,
                    action=action,
                )
                rows, total = data["groups"], len(data["groups"])
            else:
                data = retrieval.report_rows(
                    conn,
                    principal,
                    req.dataset,
                    filters,
                    person=req.person,
                    limit=limit,
                    action=action,
                )
                rows, total = data["rows"], data["total"]
                for r in rows:
                    r["sender_domain"] = (r["sender"] or "").partition("@")[2] or None
    else:
        with operator_conn() as conn:
            if req.dataset == "runs":
                rows = [
                    r
                    for r in recent_runs(conn, 500)
                    if (since is None or datetime.fromisoformat(r["started_at"]) >= since)
                    and (until is None or datetime.fromisoformat(r["started_at"]) < until)
                ]
            elif req.dataset == "mailboxes":
                rows = [
                    m.__dict__
                    for m in list_mailboxes(conn)
                    if not req.mailbox or req.mailbox.lower() in m.address
                ]
            else:
                rows = calibration.label_report(
                    conn.execute(
                        "SELECT label, applies, probability, decision FROM ops_label_calibration"
                    ).fetchall()
                )
                for r in rows:
                    r["suggested_yes"] = (r["suggestion"] or {}).get("yes_at_or_above")
            details = {"dataset": req.dataset, "rows": len(rows)}
            audit(conn, principal.oid, action, None, None, details, actor_type="user")
            conn.commit()
        total = len(rows)
        rows = rows[:limit]

    def title(c: Column) -> str:
        return GROUP_TITLES[req.group_by] if req.group_by and c.key == "key" else c.title

    columns = [
        {"key": c.key, "title": title(c), "kind": c.kind}
        for c in COLUMNS[req.kind]
        if c.key in req.columns
    ]
    return {
        "dataset": req.dataset,
        "group_by": req.group_by,
        "columns": columns,
        "rows": rows,
        "total": total,
        "truncated": total > len(rows),
        "label_mode": req.label_mode,
        "available": [{"key": c.key, "title": title(c)} for c in COLUMNS[req.kind]],
    }


# ----- files -----------------------------------------------------------------


def _safe(text: str) -> str:
    return "'" + text if text.startswith(_UNSAFE_START) else text


def _cell(col: Column, value: Any, label_mode: str, group_by: str | None, tz: ZoneInfo) -> Any:
    if value is None:
        return None
    if col.kind == "labels" or (col.key == "key" and group_by == "label"):
        items = value if isinstance(value, list) else [value]
        return ", ".join(str(v) if label_mode == "keys" else label_title(str(v)) for v in items)
    if col.kind == "datetime":
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.astimezone(tz).replace(tzinfo=None) if isinstance(value, datetime) else value
    if col.kind == "bool":
        return "Yes" if value else "No"
    if col.kind in ("number", "rate"):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in sorted(value.items()))
    return str(value)


def table(report: dict[str, Any], tz_name: str) -> tuple[list[str], list[list[Any]]]:
    tz = ZoneInfo(tz_name)
    cols = [Column(c["key"], c["title"], c["kind"]) for c in report["columns"]]
    header = [c.title for c in cols]
    body = [
        [_cell(c, r.get(c.key), report["label_mode"], report["group_by"], tz) for c in cols]
        for r in report["rows"]
    ]
    return header, body


def to_csv(header: list[str], body: list[list[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in body:
        writer.writerow(
            [
                v.strftime("%Y-%m-%d %H:%M")
                if isinstance(v, datetime)
                else _safe(v)
                if isinstance(v, str)
                else v
                for v in row
            ]
        )
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def to_xlsx(header: list[str], body: list[list[Any]], title: str) -> bytes:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title[:31] or "Report")
    ws.freeze_panes = "A2"
    bold = Font(bold=True)
    head = []
    for name in header:
        cell = WriteOnlyCell(ws, value=name)
        cell.font = bold
        head.append(cell)
    ws.append(head)
    for row in body:
        cells = []
        for v in row:
            cell = WriteOnlyCell(ws, value=v)
            if isinstance(v, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
            elif isinstance(v, str):
                cell.data_type = "s"  # never a formula, whatever the text starts with
            cells.append(cell)
        ws.append(cells)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def file_name(req: ReportRequest, fmt: str, today: date) -> str:
    part = f"{req.dataset}-by-{req.group_by}" if req.group_by else req.dataset
    return f"email-intelligence-{part}-{today.isoformat()}.{fmt}"
