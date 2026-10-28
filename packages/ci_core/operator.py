"""Operator actions: mode changes, mailbox approvals and exclusions.

Each action writes an ``audit_events`` row in the same transaction. Functions
do not commit; the caller owns the transaction. Audit details hold ids, states
and reason codes only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from ci_core.db import Connection, required
from ci_core.runtime import Mode, RuntimeConfig, active_config

Dataset = Literal["test", "production"]
_ADDRESS = re.compile(r"^[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}$")
_DOMAIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_REASON = re.compile(r"^[a-z0-9_]{3,64}$")


class OperatorError(ValueError):
    pass


@dataclass(frozen=True)
class MailboxRow:
    scope_id: str
    address: str
    recipient_type: str
    scope_state: str
    test_approved: bool
    production_approved: bool
    excluded: bool


def audit(
    conn: Connection,
    actor: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    *,
    actor_type: Literal["operator", "service", "user"] = "operator",
) -> None:
    conn.execute(
        "INSERT INTO audit_events (actor_type, actor_id, action, target_type, target_id, details)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (actor_type, actor, action, target_type, target_id, json.dumps(details or {})),
    )


def set_mode(conn: Connection, mode: Mode, actor: str, note: str | None = None) -> RuntimeConfig:
    """Activate a new revision. Does not approve scopes or enable sources (ADR-004)."""
    if mode not in ("test", "production", "paused"):
        raise OperatorError(f"unknown mode {mode!r}")
    previous = conn.execute(
        "SELECT revision, mode, enabled_providers FROM runtime_config WHERE is_active FOR UPDATE"
    ).fetchone()
    providers = list(previous["enabled_providers"]) if previous else ["email"]
    conn.execute("UPDATE runtime_config SET is_active = false WHERE is_active")
    row = required(
        conn.execute(
            "INSERT INTO runtime_config (mode, enabled_providers, is_active, activated_at,"
            " activated_by, notes) VALUES (%s, %s, true, now(), %s, %s) RETURNING revision",
            (mode, providers, actor, note),
        ).fetchone()
    )
    audit(
        conn,
        actor,
        "mode_change",
        "runtime_config",
        str(row["revision"]),
        {"from": previous["mode"] if previous else None, "to": mode},
    )
    return active_config(conn)


def list_mailboxes(conn: Connection) -> list[MailboxRow]:
    rows = conn.execute(
        """
        SELECT s.id::text AS scope_id, m.primary_smtp_address AS address, m.recipient_type,
               s.scope_state, s.test_approved, s.production_approved,
               EXISTS (SELECT 1 FROM exclusions e
                       WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL
                         AND e.scope_kind = s.scope_kind
                         AND e.native_scope_id = s.native_scope_id) AS excluded
        FROM source_scopes s JOIN email_mailboxes m ON m.scope_id = s.id
        ORDER BY m.primary_smtp_address
        """
    ).fetchall()
    return [MailboxRow(**row) for row in rows]


def list_participant_exclusions(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id::text, exclusion_kind, pattern, reason_code, created_by, created_at"
        " FROM exclusions WHERE exclusion_kind IN ('participant_address', 'participant_domain')"
        " AND revoked_at IS NULL ORDER BY pattern"
    ).fetchall()


def _mailbox(conn: Connection, address: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.id::text AS scope_id, s.scope_kind, s.native_scope_id"
        " FROM source_scopes s JOIN email_mailboxes m ON m.scope_id = s.id"
        " WHERE m.primary_smtp_address = %s",
        (address.strip().lower(),),
    ).fetchone()
    if row is None:
        raise OperatorError(f"{address} is not in the mailbox inventory; run inventory sync first")
    return row


def _reason(reason: str) -> str:
    if not _REASON.match(reason):
        raise OperatorError("reason must be a code like 'personal_mailbox' (a-z, 0-9, _)")
    return reason


def approve_mailbox(
    conn: Connection, address: str, dataset: Dataset, actor: str, *, revoke: bool = False
) -> None:
    """Set or clear the dataset approval. An active exclusion still wins."""
    mailbox = _mailbox(conn, address)
    column = {"test": "test_approved", "production": "production_approved"}[dataset]
    conn.execute(
        f"UPDATE source_scopes SET {column} = %s, approved_by = %s, approved_at = now()"  # noqa: S608
        " WHERE id = %s",
        (not revoke, actor, mailbox["scope_id"]),
    )
    action = "scope_unapprove" if revoke else "scope_approve"
    audit(conn, actor, action, "source_scope", mailbox["scope_id"], {"dataset": dataset})


def exclude_mailbox(
    conn: Connection, address: str, actor: str, *, reason: str | None = None, revoke: bool = False
) -> None:
    mailbox = _mailbox(conn, address)
    if revoke:
        updated = conn.execute(
            "UPDATE exclusions SET revoked_at = now(), revoked_by = %s"
            " WHERE exclusion_kind = 'scope' AND revoked_at IS NULL"
            " AND scope_kind = %s AND native_scope_id = %s RETURNING id::text",
            (actor, mailbox["scope_kind"], mailbox["native_scope_id"]),
        ).fetchall()
        if not updated:
            raise OperatorError(f"{address} has no active exclusion")
        audit(
            conn,
            actor,
            "scope_unexclude",
            "source_scope",
            mailbox["scope_id"],
            {"exclusion_ids": [r["id"] for r in updated]},
        )
        return
    if reason is None:
        raise OperatorError("an exclusion needs --reason")
    row = conn.execute(
        "INSERT INTO exclusions (exclusion_kind, scope_kind, native_scope_id, reason_code,"
        " created_by) VALUES ('scope', %s, %s, %s, %s)"
        " ON CONFLICT DO NOTHING RETURNING id::text",
        (mailbox["scope_kind"], mailbox["native_scope_id"], _reason(reason), actor),
    ).fetchone()
    if row is None:
        raise OperatorError(f"{address} is already excluded")
    audit(
        conn,
        actor,
        "scope_exclude",
        "source_scope",
        mailbox["scope_id"],
        {"exclusion_id": row["id"], "reason_code": reason},
    )


def parse_participant(pattern: str) -> tuple[str, str]:
    """``someone@example.com`` -> address exclusion; ``@example.com`` -> domain exclusion."""
    value = pattern.strip().lower()
    if value.startswith("@") and _DOMAIN.match(value[1:]):
        return "participant_domain", value[1:]
    if _ADDRESS.match(value):
        return "participant_address", value
    raise OperatorError("use an address (name@example.com) or a domain (@example.com)")


def exclude_participant(
    conn: Connection, pattern: str, actor: str, *, reason: str | None = None, revoke: bool = False
) -> None:
    """Hide every occurrence with this participant, in every mailbox."""
    kind, value = parse_participant(pattern)
    if revoke:
        updated = conn.execute(
            "UPDATE exclusions SET revoked_at = now(), revoked_by = %s"
            " WHERE exclusion_kind = %s AND pattern = %s AND revoked_at IS NULL RETURNING id::text",
            (actor, kind, value),
        ).fetchall()
        if not updated:
            raise OperatorError(f"no active exclusion for {pattern}")
        audit(conn, actor, "participant_unexclude", "exclusion", updated[0]["id"])
        return
    if reason is None:
        raise OperatorError("an exclusion needs --reason")
    row = conn.execute(
        "INSERT INTO exclusions (exclusion_kind, pattern, reason_code, created_by)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id::text",
        (kind, value, _reason(reason), actor),
    ).fetchone()
    if row is None:
        raise OperatorError(f"{pattern} is already excluded")
    audit(conn, actor, "participant_exclude", "exclusion", row["id"], {"reason_code": reason})
