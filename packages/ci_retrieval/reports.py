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
_RANK = "CASE {c} WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 END"
# Impact x likelihood, 1-9; NULL when unrated.
_SCORE = f"({_RANK.format(c='m.risk_impact')} * {_RANK.format(c='m.risk_likelihood')})"

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
    """
    days = max(1, min(days, 366))
    impact, likelihood = _level(impact, "impact"), _level(likelihood, "likelihood")
    end = now or datetime.now(UTC)
    start, prev_start = end - timedelta(days=days), end - timedelta(days=2 * days)
    auth = authorize(conn, principal)
    current = Filters(mailbox=mailbox, since=start, until=end, label=category, property=property)
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
    if not auth.empty:
        ratings = _has_ratings(conn)
        rating = {"impact": impact, "likelihood": likelihood}
        base = (
            _params(auth, current)
            | rating
            | {
                "risk_labels": list(RISK_LABELS),
                "tz": tz,
                "signals": list(CO_SIGNALS),
                "cat": CATEGORY_PREFIX + "%",
                "unit": "day" if days <= 14 else "week",
            }
        )
        every = f"({_messages(_RISK_WHERE, ratings=ratings)}) m"
        risky = f"""(SELECT * FROM {every}
            WHERE (%(impact)s::text IS NULL OR coalesce(m.risk_impact, 'unrated') = %(impact)s)
              AND (%(likelihood)s::text IS NULL
                   OR coalesce(m.risk_likelihood, 'unrated') = %(likelihood)s)) m"""  # noqa: S608
        out["total"] = required(
            conn.execute(f"SELECT count(*) AS n FROM {risky}", base).fetchone()  # noqa: S608
        )["n"]
        previous = (
            _params(
                auth,
                Filters(
                    mailbox=mailbox,
                    since=prev_start,
                    until=start,
                    label=category,
                    property=property,
                ),
            )
            | rating
            | {"risk_labels": list(RISK_LABELS)}
        )
        out["previous_total"] = required(
            conn.execute(f"SELECT count(*) AS n FROM {risky}", previous).fetchone()  # noqa: S608
        )["n"]
        periods = [
            r["period"]
            for r in conn.execute(
                "SELECT g::date::text AS period FROM generate_series("
                " date_trunc(%(unit)s, %(since)s AT TIME ZONE %(tz)s),"
                " date_trunc(%(unit)s, %(until)s AT TIME ZONE %(tz)s),"
                " ('1 ' || %(unit)s)::interval) g ORDER BY 1",
                base,
            ).fetchall()
        ]
        period_of = "date_trunc(%(unit)s, m.occurred_at AT TIME ZONE %(tz)s)::date::text"

        def per_period(key: str, join: str = "", where: str = "") -> dict[str, dict[str, int]]:
            """Counts per (key, period) over the filtered risk emails."""
            grid: dict[str, dict[str, int]] = {}
            for r in conn.execute(
                f"SELECT {key} AS key, {period_of} AS period, count(*) AS n"  # noqa: S608
                f" FROM {risky}{join} {where} GROUP BY 1, 2",
                base,
            ).fetchall():
                grid.setdefault(r["key"], {})[r["period"]] = int(r["n"])
            return grid

        totals = per_period("''")
        out["series"] = [{"period": p, "risks": totals.get("", {}).get(p, 0)} for p in periods]
        by_cat = per_period("l", ", unnest(m.yes_labels) l", "WHERE l LIKE %(cat)s")
        out["category_series"] = {
            "periods": periods,
            "categories": sorted(
                (
                    {"label": label, "counts": [counts.get(p, 0) for p in periods]}
                    for label, counts in by_cat.items()
                ),
                key=lambda c: (-sum(c["counts"]), c["label"]),
            ),
        }
        out["by_category"] = [
            {"label": c["label"], "risks": sum(c["counts"])}
            for c in out["category_series"]["categories"]
        ]
        out["by_company"] = [
            {"domain": r["domain"], "risks": int(r["n"])}
            for r in conn.execute(
                "SELECT coalesce(nullif(split_part(m.sender, '@', 2), ''), '(unknown)') AS domain,"  # noqa: S608
                f" count(*) AS n FROM {risky} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10",
                base,
            ).fetchall()
        ]
        top = conn.execute(
            f"""
            SELECT p AS property, count(*) AS n,
                   count(*) FILTER (WHERE m.risk_impact = 'high') AS high_impact,
                   max({_SCORE}) AS worst, max(m.occurred_at) AS last_at
            FROM {risky}, unnest(m.properties) p
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 15
            """,  # noqa: S608 - fragments are constants; values are bound parameters
            base,
        ).fetchall()
        out["by_property"] = [{"property": r["property"], "risks": int(r["n"])} for r in top]
        before = {
            r["property"]: int(r["n"])
            for r in conn.execute(
                f"SELECT p AS property, count(*) AS n FROM {risky}, unnest(m.properties) p"  # noqa: S608
                " WHERE p = ANY(%(names)s::text[]) GROUP BY 1",
                previous | {"names": [r["property"] for r in top]},
            ).fetchall()
        }
        out["top_properties"] = [
            {
                "property": r["property"],
                "risks": int(r["n"]),
                "previous": before.get(r["property"], 0),
                "high_impact": int(r["high_impact"]),
                "worst_score": int(r["worst"]) if r["worst"] is not None else None,
                "last_at": r["last_at"].isoformat(),
            }
            for r in top[:10]
        ]
        base["names"] = [r["property"] for r in top[:10]]
        heat = per_period("p", ", unnest(m.properties) p", "WHERE p = ANY(%(names)s::text[])")
        out["heatmap"] = {
            "periods": periods,
            "rows": [
                {
                    "property": r["property"],
                    "counts": [heat.get(r["property"], {}).get(p, 0) for p in periods],
                }
                for r in top[:10]
            ],
        }
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
        # The matrix ignores the impact/likelihood filter so every cell stays visible.
        cells = {
            (r["impact"], r["likelihood"]): int(r["n"])
            for r in conn.execute(
                f"SELECT coalesce(m.risk_impact, 'unrated') AS impact,"  # noqa: S608
                f" coalesce(m.risk_likelihood, 'unrated') AS likelihood, count(*) AS n"
                f" FROM {every} GROUP BY 1, 2",
                base,
            ).fetchall()
        }
        unrated = sum(n for (i, _), n in cells.items() if i == UNRATED)
        out["matrix"] = {
            "rated": sum(cells.values()) - unrated,
            "unrated": unrated,
            "cells": [
                {"impact": i, "likelihood": li, "risks": cells.get((i, li), 0)}
                for i in reversed(RATING_LEVELS)
                for li in RATING_LEVELS
            ],
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
