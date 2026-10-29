"""Reports, risk analysis and the label-review queue, over published email.

Same rules as ``search``: runs as ``ci_search`` against the ``published_*``
views, the caller's grants are applied in SQL before anything else, caller
filters only narrow, copies of one message are collapsed after authorization,
and every call is audited (ids and counts, never text). Rows carry metadata
and AI proposals only: subject, sender, mailbox, date, labels, summary, action
and risk text. Email bodies and attachment text are never part of a report.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from ci_core.db import Connection
from ci_retrieval.identity import Principal
from ci_retrieval.search import (
    _AUTH,
    _PUBLISHED,
    ACTION_LABELS,
    ACTION_RULE_WHERE,
    NO_FILTERS,
    RISK_LABELS,
    ROUTED_WHERE,
    Filters,
    _audit,
    _params,
    _scope,
    action_rule_params,
    authorize,
    person_profile,
)

Dataset = Literal["messages", "actions", "risks"]
DATASETS: tuple[Dataset, ...] = ("messages", "actions", "risks")
GROUPS = (
    "mailbox",
    "sender",
    "sender_domain",
    "label",
    "property",
    "entity",
    "day",
    "week",
    "month",
)
REVIEW_MODES = ("random", "uncertain", "label")
MAX_ROWS = 5000
CATEGORY_PREFIX = "cat_"
CO_SIGNALS = ("urgent", "approval_required", "action_required", "invoice_receipt")
RATING_LEVELS = ("low", "medium", "high")
UNRATED = "unrated"

_RISK_WHERE = "AND cl.yes_labels && %(risk_labels)s::text[]"


def _messages(extra_where: str, keep_copies: bool = False, ratings: bool = False) -> str:
    """Authorized, filtered messages; one row per message unless ``keep_copies``.

    ``ratings``: join the AI risk ratings (migration 0017); without it the rating
    columns are NULL."""
    copy = ", pc.id" if keep_copies else ""
    if ratings:
        rated = (
            "rr.impact AS risk_impact, rr.likelihood AS risk_likelihood, rr.reason AS risk_reason"
        )
        rating_join = "LEFT JOIN published_risk_ratings rr ON rr.communication_id = pc.id"
    else:
        rated = (
            "NULL::text AS risk_impact, NULL::text AS risk_likelihood, NULL::text AS risk_reason"
        )
        rating_join = ""
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
               coalesce(cl.properties, '{{}}') AS properties,
               coalesce(cl.entities, '{{}}') AS entities,
               (SELECT count(*) FROM published_email_attachments a
                 WHERE a.communication_id = pc.id
                   AND a.extraction_status <> 'skipped') AS attachments,
               {rated}
        FROM {_PUBLISHED} pc
        JOIN published_email_messages e ON e.communication_id = pc.id
        LEFT JOIN published_classifications cl ON cl.communication_id = pc.id
        {rating_join}
        WHERE {_scope("pc")} {extra_where}
        ORDER BY coalesce(e.internet_message_id, pc.id::text),
                 coalesce(e.content_fingerprint, ''){copy}, pc.occurred_at DESC, pc.id
    """  # noqa: S608 - fragments are constants; values are bound parameters


def _dataset_where(
    conn: Connection, dataset: str, person: str | None
) -> tuple[str, dict[str, Any]]:
    """Extra WHERE and parameters for a dataset.

    Action items follow ``list_actionable`` exactly: the person's step-2 check
    when they have a profile, otherwise the step-1 rule.
    """
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {', '.join(DATASETS)}")
    people = sorted({a.strip().lower() for a in (person or "").split(",") if a.strip()}) or None
    extra: dict[str, Any] = {"risk_labels": list(RISK_LABELS), "action_labels": list(ACTION_LABELS)}
    if dataset == "risks":
        return _RISK_WHERE, extra
    if dataset == "actions":
        profile = person_profile(conn, person)
        if profile is not None:
            return ROUTED_WHERE, extra | {"person_key": profile["key"], "decision": "yes"}
        return ACTION_RULE_WHERE, extra | action_rule_params(people)
    return "", extra


def _has_ratings(conn: Connection) -> bool:
    """True once migration 0017 (AI risk ratings) is applied."""
    row = conn.execute(
        "SELECT to_regclass('ci.published_risk_ratings') IS NOT NULL AS ok"
    ).fetchone()
    return bool(row and row["ok"])


def _row(r: dict[str, Any]) -> dict[str, Any]:
    row = {
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
        "properties_ai": list(r["properties"]),
        "entities_ai": list(r["entities"]),
        "attachments": int(r["attachments"]),
        "web_link": r["source_url"],
    }
    if r.get("risk_impact"):
        row["risk_rating_ai"] = {
            "impact": r["risk_impact"],
            "likelihood": r["risk_likelihood"],
            "reason": r["risk_reason"],
        }
    return row


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
    where, extra = _dataset_where(conn, dataset, person)
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
        "property": (", unnest(m.properties) AS prop", "prop"),
        "entity": (", unnest(m.entities) AS ent", "ent"),
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
    where, extra = _dataset_where(conn, dataset, person)
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


def _level(value: str | None, name: str) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip().lower()
    if value not in (*RATING_LEVELS, UNRATED):
        raise ValueError(f"{name} must be low, medium, high or unrated")
    return value


def _has_label_lookup(conn: Connection) -> bool:
    """True once migration 0021 (the index-backed published_labelled) is applied."""
    row = conn.execute(
        "SELECT to_regprocedure('ci.published_labelled(text[], timestamptz, timestamptz)')"
        " IS NOT NULL AS ok"
    ).fetchone()
    return bool(row and row["ok"])


# Risk emails in a time range, one row per message (copies collapsed after the
# grants apply), with every column _row shows. From the label index
# (migration 0021); _RISK_ROWS_SLOW is the same through the views, for a
# database without it.
_RISK_ROWS = f"""
    SELECT DISTINCT ON (coalesce(h.internet_message_id, h.communication_id::text),
                        coalesce(h.content_fingerprint, ''))
           h.communication_id::text AS communication_id, h.occurred_at, h.title, h.source_url,
           h.mailbox_address, h.direction, h.sender, h.yes_labels, h.uncertain_labels,
           h.disposition, h.summary, h.action_text, h.action_for, h.due_text, h.risk_text,
           h.properties, h.entities, h.attachments, h.risk_impact, h.risk_likelihood,
           h.risk_reason
    FROM published_labelled(%(risk_labels)s::text[], %(since)s, %(until)s) h
    WHERE {_AUTH.format(a="h")}
      AND h.occurred_at >= %(since)s AND h.occurred_at < %(until)s
      AND (%(mailbox)s::text IS NULL OR h.mailbox_address = %(mailbox)s)
    ORDER BY coalesce(h.internet_message_id, h.communication_id::text),
             coalesce(h.content_fingerprint, ''), h.occurred_at DESC, h.communication_id
"""  # noqa: S608 - fragments are constants; values are bound parameters

_RANK = {"low": 1, "medium": 2, "high": 3}


def _domain(sender: str | None) -> str:
    part = sender.split("@")[1] if sender and "@" in sender else ""
    return part or "(unknown)"


def _top(counts: dict[str, int], n: int) -> list[tuple[str, int]]:
    """Largest first, ties by name (the reports' ORDER BY count DESC, key)."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def risk_overview(
    conn: Connection,
    principal: Principal,
    *,
    days: int = 30,
    mailbox: str | None = None,
    category: str | None = None,
    property: str | None = None,
    impact: str | None = None,
    likelihood: str | None = None,
    tz: str = "America/Chicago",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project-risk emails in a window, with counts by period, category, property,
    company and mailbox, and the impact x likelihood matrix.

    Built from stored labels, the AI's one-line risk text, the property names it
    extracted (as written in the email, or the registry's canonical name) and the
    AI's impact and likelihood rating of each risk email (migration 0017). The
    ratings are uncalibrated AI proposals; emails not rated yet count as unrated.
    ``impact``/``likelihood`` (low, medium, high or unrated) narrow everything
    except the matrix, which always shows every cell of the other filters.

    One query reads the risk emails of this window and the one before (from the
    label index, migration 0021); every count and the newest 200 for the feed are
    made from those rows.
    """
    days = max(1, min(days, 366))
    impact, likelihood = _level(impact, "impact"), _level(likelihood, "likelihood")
    end = now or datetime.now(UTC)
    start, prev_start = end - timedelta(days=days), end - timedelta(days=2 * days)
    zone = ZoneInfo(tz)
    unit = "day" if days <= 14 else "week"
    out: dict[str, Any] = {
        "days": days,
        "since": start.isoformat(),
        "total": 0,
        "previous_total": 0,
        "series": [],
        "by_category": [],
        "by_company": [],
        "by_mailbox": [],
        "by_property": [],
        "co_signals": {},
        "matrix": {"rated": 0, "unrated": 0, "cells": []},
        "category_series": {"periods": [], "categories": []},
        "heatmap": {"periods": [], "rows": []},
        "top_properties": [],
        "items": [],
    }
    auth = authorize(conn, principal)
    if not auth.empty:
        ratings = _has_ratings(conn)
        p = _params(auth, Filters(mailbox=mailbox, since=prev_start, until=end)) | {
            "risk_labels": list(RISK_LABELS)
        }
        fast = ratings and _has_label_lookup(conn)
        sql = _RISK_ROWS if fast else f"SELECT * FROM ({_messages(_RISK_WHERE, ratings=ratings)}) m"  # noqa: S608
        found = conn.execute(sql, p).fetchall()
        prop = property.strip().casefold() if property and property.strip() else None

        def keep(r: dict[str, Any]) -> bool:  # category and property, as _FILTERS had them
            if category and category not in r["yes_labels"]:
                return False
            return prop is None or any(prop in (n or "").casefold() for n in r["properties"])

        def rated(r: dict[str, Any]) -> bool:
            return (impact is None or (r["risk_impact"] or UNRATED) == impact) and (
                likelihood is None or (r["risk_likelihood"] or UNRATED) == likelihood
            )

        every = [r for r in found if keep(r) and r["occurred_at"] >= start]
        current = [r for r in every if rated(r)]
        previous = [r for r in found if keep(r) and rated(r) and r["occurred_at"] < start]
        out["total"], out["previous_total"] = len(current), len(previous)

        def bucket(at: datetime) -> date:
            local = at.astimezone(zone).date()
            return local - timedelta(days=local.weekday()) if unit == "week" else local

        first, last = bucket(start), bucket(end)
        step = timedelta(days=7 if unit == "week" else 1)
        period_days = [first + step * i for i in range((last - first) // step + 1)]
        periods = [d.isoformat() for d in period_days]
        per_total: dict[str, int] = {}
        per_cat: dict[str, dict[str, int]] = {}
        companies: dict[str, int] = {}
        mailboxes: dict[str, int] = {}
        signals: dict[str, int] = {}
        props: dict[str, int] = {}
        for r in current:
            period = bucket(r["occurred_at"]).isoformat()
            per_total[period] = per_total.get(period, 0) + 1
            for label in r["yes_labels"]:
                if label.startswith(CATEGORY_PREFIX):
                    counts = per_cat.setdefault(label, {})
                    counts[period] = counts.get(period, 0) + 1
                if label in CO_SIGNALS:
                    signals[label] = signals.get(label, 0) + 1
            domain = _domain(r["sender"])
            companies[domain] = companies.get(domain, 0) + 1
            mailboxes[r["mailbox_address"]] = mailboxes.get(r["mailbox_address"], 0) + 1
            for name in r["properties"]:
                props[name] = props.get(name, 0) + 1
        out["series"] = [{"period": d, "risks": per_total.get(d, 0)} for d in periods]
        out["category_series"] = {
            "periods": periods,
            "categories": sorted(
                (
                    {"label": label, "counts": [counts.get(d, 0) for d in periods]}
                    for label, counts in per_cat.items()
                ),
                key=lambda c: (-sum(c["counts"]), c["label"]),
            ),
        }
        out["by_category"] = [
            {"label": c["label"], "risks": sum(c["counts"])}
            for c in out["category_series"]["categories"]
        ]
        out["by_company"] = [{"domain": k, "risks": n} for k, n in _top(companies, 10)]
        out["by_mailbox"] = [{"mailbox": k, "risks": n} for k, n in _top(mailboxes, 10)]
        out["co_signals"] = signals
        top = _top(props, 15)
        out["by_property"] = [{"property": k, "risks": n} for k, n in top]
        names = {k for k, _ in top[:10]}
        detail: dict[str, dict[str, Any]] = {
            k: {"high": 0, "worst": None, "last": None} for k in names
        }
        heat: dict[str, dict[str, int]] = {k: {} for k in names}
        for r in current:
            score = (
                _RANK[r["risk_impact"]] * _RANK[r["risk_likelihood"]]
                if r["risk_impact"] and r["risk_likelihood"]
                else None
            )
            period = bucket(r["occurred_at"]).isoformat()
            for name in r["properties"]:
                if name not in names:
                    continue
                d = detail[name]
                d["high"] += r["risk_impact"] == "high"
                if score is not None and (d["worst"] is None or score > d["worst"]):
                    d["worst"] = score
                if d["last"] is None or r["occurred_at"] > d["last"]:
                    d["last"] = r["occurred_at"]
                heat[name][period] = heat[name].get(period, 0) + 1
        before: dict[str, int] = {}
        for r in previous:
            for name in r["properties"]:
                if name in names:
                    before[name] = before.get(name, 0) + 1
        out["top_properties"] = [
            {
                "property": k,
                "risks": n,
                "previous": before.get(k, 0),
                "high_impact": detail[k]["high"],
                "worst_score": detail[k]["worst"],
                "last_at": detail[k]["last"].isoformat(),
            }
            for k, n in top[:10]
        ]
        out["heatmap"] = {
            "periods": periods,
            "rows": [
                {"property": k, "counts": [heat[k].get(d, 0) for d in periods]} for k, _ in top[:10]
            ],
        }
        # The matrix ignores the impact/likelihood filter so every cell stays visible.
        cells: dict[tuple[str, str], int] = {}
        for r in every:
            key = (r["risk_impact"] or UNRATED, r["risk_likelihood"] or UNRATED)
            cells[key] = cells.get(key, 0) + 1
        unrated = sum(n for (i, _), n in cells.items() if i == UNRATED)
        out["matrix"] = {
            "rated": len(every) - unrated,
            "unrated": unrated,
            "cells": [
                {"impact": i, "likelihood": li, "risks": cells.get((i, li), 0)}
                for i in reversed(RATING_LEVELS)
                for li in RATING_LEVELS
            ],
        }
        newest = sorted(
            current, key=lambda r: (r["occurred_at"], r["communication_id"]), reverse=True
        )[:200]
        out["items"] = [_row(r) for r in newest]
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
