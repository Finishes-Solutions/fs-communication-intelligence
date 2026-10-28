"""Metered AI usage priced with the editable unit prices (``ci.ops_usage_cost``).

This is the console's live estimate. Azure's billed figures come separately
from ``ci_admin.billing``; showing both lets the prices be checked and
corrected. Counts and dollars only.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ci_core.db import Connection
from ci_core.operator import OperatorError, audit

# Run outcome counts that are messages (ci_email.process dispositions, plus failures).
DISPOSITIONS = ("index", "review", "not_indexed", "restricted", "held", "failed")
DAYS = 62


def _usd(value: Any) -> float:
    return round(float(value or 0), 4)


def metered(conn: Connection, time_zone: str) -> dict[str, Any]:
    tz = {"tz": time_zone, "days": DAYS}
    by_day = conn.execute(
        "SELECT (recorded_at AT TIME ZONE %(tz)s)::date AS day, meter, sum(calls) AS calls,"
        " sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,"
        " sum(records) AS records, sum(estimated_usd) AS usd FROM ops_usage_cost"
        " WHERE recorded_at > now() - make_interval(days => %(days)s)"
        " GROUP BY 1, 2 ORDER BY 1 DESC, 2",
        tz,
    ).fetchall()
    totals = conn.execute(
        "SELECT coalesce(sum(estimated_usd) FILTER (WHERE recorded_at >="
        "  date_trunc('month', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s), 0) AS month,"
        " coalesce(sum(estimated_usd) FILTER (WHERE recorded_at >="
        "  date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s), 0) AS today"
        " FROM ops_usage_cost",
        tz,
    ).fetchone()
    runs = conn.execute(
        "SELECT r.id, r.trigger, r.started_at, r.finished_at, r.status, r.counts,"
        " coalesce(sum(c.estimated_usd), 0) AS usd,"
        " coalesce(jsonb_object_agg(c.meter, c.estimated_usd) FILTER (WHERE c.meter IS NOT NULL),"
        "  '{}') AS by_meter"
        " FROM ops_runs r LEFT JOIN ops_usage_cost c ON c.run_id = r.id"
        " WHERE r.kind = 'process' AND r.started_at > now() - make_interval(days => %(days)s)"
        " GROUP BY r.id ORDER BY r.started_at DESC LIMIT 50",
        tz,
    ).fetchall()
    month_messages = conn.execute(
        "SELECT counts FROM ops_runs WHERE kind = 'process' AND started_at >="
        " date_trunc('month', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s",
        tz,
    ).fetchall()
    messages = sum(_messages(r["counts"]) for r in month_messages)
    month = _usd(totals["month"] if totals else 0)
    return {
        "available": True,
        "month_usd": month,
        "today_usd": _usd(totals["today"] if totals else 0),
        "month_messages": messages,
        "per_1000_messages_usd": round(month / messages * 1000, 2) if messages else None,
        "by_day": [
            {
                "date": r["day"].isoformat(),
                "meter": r["meter"],
                "calls": int(r["calls"]),
                "input_tokens": int(r["input_tokens"]),
                "output_tokens": int(r["output_tokens"]),
                "records": int(r["records"]),
                "usd": _usd(r["usd"]),
            }
            for r in by_day
        ],
        "runs": [
            {
                "id": r["id"],
                "trigger": r["trigger"],
                "started_at": r["started_at"].isoformat(),
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "status": r["status"],
                "messages": _messages(r["counts"]),
                "usd": _usd(r["usd"]),
                "by_meter": {k: _usd(v) for k, v in (r["by_meter"] or {}).items()},
            }
            for r in runs
        ],
    }


def _messages(counts: dict[str, Any] | None) -> int:
    return sum(int((counts or {}).get(k) or 0) for k in DISPOSITIONS)


def prices(conn: Connection) -> list[dict[str, Any]]:
    return [
        {**r, "usd": float(r["usd"]), "updated_at": r["updated_at"].isoformat()}
        for r in conn.execute(
            "SELECT key, label, per_units, unit, usd, verified, updated_at, updated_by"
            " FROM cost_prices ORDER BY key"
        ).fetchall()
    ]


def update_prices(conn: Connection, actor: str, changes: dict[str, Any]) -> list[dict[str, Any]]:
    """``{key: {"usd": 0.44, "verified": true}}``; audited with the new values."""
    known = {p["key"] for p in prices(conn)}
    unknown = set(changes) - known
    if unknown:
        raise OperatorError(f"unknown price keys: {', '.join(sorted(unknown))}")
    applied: dict[str, Any] = {}
    for key, change in changes.items():
        if not isinstance(change, dict):
            raise OperatorError("each price is an object with usd and verified")
        try:
            usd = Decimal(str(change["usd"]))
        except (KeyError, ArithmeticError, ValueError) as exc:
            raise OperatorError(f"{key}: usd must be a number") from exc
        if not Decimal(0) <= usd < Decimal(10_000):
            raise OperatorError(f"{key}: usd must be between 0 and 10,000")
        verified = bool(change.get("verified", False))
        conn.execute(
            "UPDATE cost_prices SET usd = %s, verified = %s, updated_at = now(), updated_by = %s"
            " WHERE key = %s",
            (usd, verified, actor, key),
        )
        applied[key] = {"usd": str(usd), "verified": verified}
    if applied:
        audit(conn, actor, "cost_prices_change", "cost_prices", None, applied)
    return prices(conn)
