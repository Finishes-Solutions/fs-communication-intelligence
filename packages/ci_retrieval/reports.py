"""Reports, risk analysis and the label-review queue, over published email.

Same rules as ``search``: runs as ``ci_search`` against the ``published_*``
views, the caller's grants are applied in SQL before anything else, caller
filters only narrow, copies of one message are collapsed after authorization,
and every call is audited (ids and counts, never text). Rows carry metadata
and AI proposals only: subject, sender, mailbox, date, labels, summary, action
and risk text. Email bodies and attachment text are never part of a report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ci_core.db import Connection, required
from ci_retrieval.identity import Principal
from ci_retrieval.search import (
    _AUTH,
    _PUBLISHED,
    ACTION_LABELS,
    NO_FILTERS,
    RISK_LABELS,
    Filters,
    _audit,
    _params,
    _scope,
    authorize,
)

Dataset = Literal["messages", "actions", "risks"]
DATASETS: tuple[Dataset, ...] = ("messages", "actions", "risks")
GROUPS = ("mailbox", "sender", "sender_domain", "label", "day", "week", "month")
REVIEW_MODES = ("random", "uncertain", "label")
MAX_ROWS = 5000
CATEGORY_PREFIX = "cat_"
CO_SIGNALS = ("urgent", "approval_required", "action_required", "invoice_receipt")

_ACTION_WHERE = """AND cl.yes_labels && %(action_labels)s::text[]
    AND (%(people)s::text[] IS NULL OR cl.action_for && %(people)s::text[]
         OR (cardinality(cl.action_for) = 0
             AND e.mailbox_address = ANY(%(people)s::text[])
             AND e.direction = 'in'))"""
_RISK_WHERE = "AND cl.yes_labels && %(risk_labels)s::text[]"


def _messages(extra_where: str, keep_copies: bool = False) -> str:
    """Authorized, filtered messages; one row per message unless ``keep_copies``."""
    copy = ", pc.id" if keep_copies else ""
    return f"""
        SELECT DISTINCT ON (coalesce(e.internet_message_id, pc.id::text),
                            coalesce(e.content_fingerprint, ''){copy})
               pc.id::text AS communication_id, pc.title, pc.occurred_at, pc.source_url,
               e.mailbox_address, e.direction,
               (SELECT p.email_normalized FROM published_participants p
                 WHERE p.communication_id = pc.id AND p.role = 'from' LIMIT 1) AS sender,
               coalesce(cl.yes_labels, '{{}}') AS yes_labels,
               coalesce(cl.uncertain_labels, '{{}}') AS uncertain_labels,
               cl.disposition, cl.summary, cl.action_text,
               coalesce(cl.action_for, '{{}}') AS action_for, cl.due_text, cl.risk_text,
               (SELECT count(*) FROM published_email_attachments a
                 WHERE a.communication_id = pc.id
                   AND a.extraction_status <> 'skipped') AS attachments
        FROM {_PUBLISHED} pc
        JOIN published_email_messages e ON e.communication_id = pc.id
        LEFT JOIN published_classifications cl ON cl.communication_id = pc.id
        WHERE {_scope("pc")} {extra_where}
        ORDER BY coalesce(e.internet_message_id, pc.id::text),
                 coalesce(e.content_fingerprint, ''){copy}, pc.occurred_at DESC, pc.id
    """  # noqa: S608 - fragments are constants; values are bound parameters


def _dataset_where(dataset: str, person: str | None) -> tuple[str, dict[str, Any]]:
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {', '.join(DATASETS)}")
    people = sorted({a.strip().lower() for a in (person or "").split(",") if a.strip()}) or None
    extra = {
        "action_labels": list(ACTION_LABELS),
        "risk_labels": list(RISK_LABELS),
        "people": people,
    }
    where = {"messages": "", "actions": _ACTION_WHERE, "risks": _RISK_WHERE}[dataset]
    return where, extra


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "communication_id": r["communication_id"],
        "received_at": r["occurred_at"].isoformat(),
        "mailbox": r["mailbox_address"],
        "direction": r["direction"],
        "sender": r["sender"],
        "subject": r["title"],
        "labels": list(r["yes_labels"]),
        "uncertain_labels": list(r["uncertain_labels"]),
        "review_flag": r["disposition"] == "review",
        "summary_ai": r["summary"],
        "action_ai": r["action_text"],
        "action_for_ai": list(r["action_for"]),
        "due_ai": r["due_text"],
        "risk_ai": r["risk_text"],
        "attachments": int(r["attachments"]),
        "web_link": r["source_url"],
    }


def report_rows(
    conn: Connection,
    principal: Principal,
    dataset: str,
    filters: Filters = NO_FILTERS,
    *,
    person: str | None = None,
    limit: int = 200,
    action: str = "report_rows",
) -> dict[str, Any]:
    """Rows of a report, newest first, with the total before the limit."""
    limit = max(1, min(limit, MAX_ROWS))
    where, extra = _dataset_where(dataset, person)
    auth = authorize(conn, principal)
    rows: list[dict[str, Any]] = []
    total = 0
    if not auth.empty:
        found = conn.execute(
            f"SELECT m.*, count(*) OVER () AS total FROM ({_messages(where)}) m"  # noqa: S608
            " ORDER BY m.occurred_at DESC, m.communication_id LIMIT %(limit)s",
            _params(auth, filters) | extra | {"limit": limit},
        ).fetchall()
        rows = [_row(r) for r in found]
        total = int(found[0]["total"]) if found else 0
    _audit(
        conn,
        principal,
        action,
        {
            "dataset": dataset,
            "result_ids": [r["communication_id"] for r in rows],
            "total": total,
            "filters": sorted(k for k, v in filters.__dict__.items() if v is not None),
        },
    )
    conn.commit()
    return {"dataset": dataset, "total": total, "rows": rows, "truncated": total > len(rows)}


def _group_sql(group_by: str) -> tuple[str, str]:
    """(FROM addition, key expression) for a grouping over alias ``m``."""
    local = "(m.occurred_at AT TIME ZONE %(tz)s)"
    return {
        "mailbox": ("", "m.mailbox_address"),
        "sender": ("", "coalesce(m.sender, '(unknown)')"),
        "sender_domain": ("", "coalesce(nullif(split_part(m.sender, '@', 2), ''), '(unknown)')"),
        "label": (", unnest(m.yes_labels) AS lbl", "lbl"),
        "day": ("", f"date_trunc('day', {local})::date::text"),
        "week": ("", f"date_trunc('week', {local})::date::text"),
        "month": ("", f"date_trunc('month', {local})::date::text"),
    }[group_by]


def report_groups(
    conn: Connection,
    principal: Principal,
    dataset: str,
    group_by: str,
    filters: Filters = NO_FILTERS,
    *,
    person: str | None = None,
    tz: str = "America/Chicago",
    action: str = "report_groups",
) -> dict[str, Any]:
    """Message counts per group, with action-item and risk counts inside each group.

    Grouping by mailbox counts every authorized copy (each mailbox holds its
    own); every other grouping counts a message once.
    """
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    where, extra = _dataset_where(dataset, person)
    join, key = _group_sql(group_by)
    auth = authorize(conn, principal)
    groups: list[dict[str, Any]] = []
    if not auth.empty:
        found = conn.execute(
            f"""
            SELECT {key} AS key, count(*) AS messages,
                   count(*) FILTER (WHERE m.yes_labels && %(action_labels)s::text[]) AS actions,
                   count(*) FILTER (WHERE m.yes_labels && %(risk_labels)s::text[]) AS risks,
                   count(*) FILTER (WHERE m.disposition = 'review') AS flagged,
                   min(m.occurred_at) AS first_at, max(m.occurred_at) AS last_at
            FROM ({_messages(where, keep_copies=group_by == "mailbox")}) m {join}
            GROUP BY 1
            ORDER BY {"1 DESC" if group_by in ("day", "week", "month") else "2 DESC, 1"}
            LIMIT 1000
            """,  # noqa: S608 - fragments are constants; values are bound parameters
            _params(auth, filters) | extra | {"tz": tz},
        ).fetchall()
        groups = [
            {
                "key": g["key"],
                "messages": int(g["messages"]),
                "actions": int(g["actions"]),
                "risks": int(g["risks"]),
                "flagged": int(g["flagged"]),
                "first_at": g["first_at"].isoformat(),
                "last_at": g["last_at"].isoformat(),
            }
            for g in found
        ]
    _audit(
        conn, principal, action, {"dataset": dataset, "group_by": group_by, "groups": len(groups)}
    )
    conn.commit()
    return {"dataset": dataset, "group_by": group_by, "groups": groups}


def risk_overview(
    conn: Connection,
    principal: Principal,
    *,
    days: int = 30,
    mailbox: str | None = None,
    category: str | None = None,
    tz: str = "America/Chicago",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project-risk emails in a window, with counts by week, category, company and mailbox.

    Built only from stored labels and the AI's one-line risk text: there is no
    project, risk type or severity yet, so nothing here groups by those.
    """
    days = max(1, min(days, 366))
    end = now or datetime.now(UTC)
    start, prev_start = end - timedelta(days=days), end - timedelta(days=2 * days)
    auth = authorize(conn, principal)
    current = Filters(mailbox=mailbox, since=start, until=end, label=category)
    out: dict[str, Any] = {
        "days": days,
        "since": start.isoformat(),
        "total": 0,
        "previous_total": 0,
        "series": [],
        "by_category": [],
        "by_company": [],
        "by_mailbox": [],
        "co_signals": {},
        "items": [],
    }
    if not auth.empty:
        base = _params(auth, current) | {
            "risk_labels": list(RISK_LABELS),
            "tz": tz,
            "signals": list(CO_SIGNALS),
            "cat": CATEGORY_PREFIX + "%",
            "unit": "day" if days <= 14 else "week",
        }
        risky = f"({_messages(_RISK_WHERE)}) m"
        out["total"] = required(
            conn.execute(f"SELECT count(*) AS n FROM {risky}", base).fetchone()  # noqa: S608
        )["n"]
        previous = _params(
            auth, Filters(mailbox=mailbox, since=prev_start, until=start, label=category)
        )
        out["previous_total"] = required(
            conn.execute(
                f"SELECT count(*) AS n FROM {risky}",  # noqa: S608
                previous | {"risk_labels": list(RISK_LABELS)},
            ).fetchone()
        )["n"]
        out["series"] = [
            {"period": r["period"], "risks": int(r["n"])}
            for r in conn.execute(
                # Every period in the window, including the ones with no risks.
                f"""
                WITH counts AS (
                    SELECT date_trunc(%(unit)s, m.occurred_at AT TIME ZONE %(tz)s) AS p,
                           count(*) AS n
                    FROM {risky} GROUP BY 1)
                SELECT g::date::text AS period, coalesce(c.n, 0) AS n
                FROM generate_series(date_trunc(%(unit)s, %(since)s AT TIME ZONE %(tz)s),
                                     date_trunc(%(unit)s, %(until)s AT TIME ZONE %(tz)s),
                                     ('1 ' || %(unit)s)::interval) g
                LEFT JOIN counts c ON c.p = g
                ORDER BY 1
                """,  # noqa: S608 - fragments are constants; values are bound parameters
                base,
            ).fetchall()
        ]
        out["by_category"] = [
            {"label": r["label"], "risks": int(r["n"])}
            for r in conn.execute(
                f"SELECT l AS label, count(*) AS n FROM {risky}, unnest(m.yes_labels) l"  # noqa: S608
                " WHERE l LIKE %(cat)s GROUP BY 1 ORDER BY 2 DESC, 1",
                base,
            ).fetchall()
        ]
        out["by_company"] = [
            {"domain": r["domain"], "risks": int(r["n"])}
            for r in conn.execute(
                "SELECT coalesce(nullif(split_part(m.sender, '@', 2), ''), '(unknown)') AS domain,"  # noqa: S608
                f" count(*) AS n FROM {risky} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10",
                base,
            ).fetchall()
        ]
        out["by_mailbox"] = [
            {"mailbox": r["mailbox_address"], "risks": int(r["n"])}
            for r in conn.execute(
                f"SELECT m.mailbox_address, count(*) AS n FROM {risky}"  # noqa: S608
                " GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10",
                base,
            ).fetchall()
        ]
        out["co_signals"] = {
            r["label"]: int(r["n"])
            for r in conn.execute(
                f"SELECT l AS label, count(*) AS n FROM {risky}, unnest(m.yes_labels) l"  # noqa: S608
                " WHERE l = ANY(%(signals)s::text[]) GROUP BY 1",
                base,
            ).fetchall()
        }
        items = conn.execute(
            f"SELECT m.* FROM {risky} ORDER BY m.occurred_at DESC LIMIT 200",  # noqa: S608
            base,
        ).fetchall()
        out["items"] = [_row(r) for r in items]
    _audit(
        conn,
        principal,
        "risk_overview",
        {"result_ids": [i["communication_id"] for i in out["items"]], "total": out["total"]},
    )
    conn.commit()
    return out


def review_queue(
    conn: Connection,
    principal: Principal,
    *,
    mode: str,
    label: str | None = None,
    exclude: list[str] | None = None,
    since_days: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Messages to hand-review, skipping ``exclude`` (already reviewed by this person).

    ``random`` is an unbiased sample: only reviews of random picks give honest
    precision and recall. ``uncertain`` picks messages with a label in the
    review band; ``label`` picks messages the AI marked yes or uncertain for
    one label. Both help tune thresholds but over-represent hard cases.
    """
    if mode not in REVIEW_MODES:
        raise ValueError(f"mode must be one of {', '.join(REVIEW_MODES)}")
    if mode == "label" and not label:
        raise ValueError("pick a label to review")
    since = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    where = {
        "random": "",
        "uncertain": "AND cardinality(cl.uncertain_labels) > 0",
        "label": "AND (%(rlabel)s = ANY(cl.yes_labels) OR %(rlabel)s = ANY(cl.uncertain_labels))",
    }[mode]
    auth = authorize(conn, principal)
    rows: list[dict[str, Any]] = []
    if not auth.empty:
        found = conn.execute(
            f"SELECT m.* FROM ({_messages(where)}) m"  # noqa: S608
            " WHERE NOT (m.communication_id = ANY(%(exclude)s::text[]))"
            f" ORDER BY {'random()' if mode != 'uncertain' else 'm.occurred_at DESC'}"
            " LIMIT %(limit)s",
            _params(auth, Filters(since=since))
            | {"rlabel": label, "exclude": exclude or [], "limit": max(1, min(limit, 50))},
        ).fetchall()
        rows = [_row(r) for r in found]
    _audit(
        conn,
        principal,
        "review_queue",
        {"mode": mode, "result_ids": [r["communication_id"] for r in rows]},
    )
    conn.commit()
    return {"mode": mode, "label": label, "items": rows}


def message_labels(
    conn: Connection, principal: Principal, communication_id: str
) -> dict[str, Any] | None:
    """The classifier's per-label output for one message, if the caller may read it."""
    auth = authorize(conn, principal)
    rows = []
    if not auth.empty:
        rows = conn.execute(
            "SELECT l.run_id, l.model, l.policy_version, l.label, l.probability, l.decision"  # noqa: S608
            f" FROM published_classification_labels l"
            f" WHERE l.communication_id = %(id)s::uuid AND {_AUTH.format(a='l')}",
            _params(auth, Filters()) | {"id": communication_id},
        ).fetchall()
    _audit(
        conn,
        principal,
        "message_labels",
        {"found": bool(rows), "result_ids": [communication_id] if rows else []},
    )
    conn.commit()
    if not rows:
        return None
    first = rows[0]
    return {
        "communication_id": communication_id,
        "run_id": first["run_id"],
        "model": first["model"],
        "policy_version": first["policy_version"],
        "labels": {
            r["label"]: {"probability": r["probability"], "decision": r["decision"]} for r in rows
        },
    }
