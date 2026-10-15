"""Runtime mode, scope approvals and exclusions (the database "soft layer").

Reads run as ``ci_ingest``; every change runs as ``ci_operator`` and writes an
``audit_events`` row. Mode changes create a new ``runtime_config`` revision;
the unique index allows exactly one active revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from psycopg.types.json import Jsonb

from ci_core.db import Connection, DbRole, many, one, one_required, transaction

Mode = Literal["test", "production", "paused"]
ExclusionKind = Literal["scope", "participant_address", "participant_domain", "item"]


class ControlError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeState:
    revision: int
    mode: Mode
    enabled_providers: tuple[str, ...]

    @property
    def dataset_key(self) -> str | None:
        return None if self.mode == "paused" else self.mode

    def allows(self, provider: str) -> bool:
        return self.dataset_key is not None and provider in self.enabled_providers


def runtime_state(conn: Connection, role: DbRole = "ci_ingest") -> RuntimeState | None:
    """The active revision, or None when none exists (which fails closed)."""
    with transaction(conn, role):
        row = one(
            conn, "SELECT revision, mode, enabled_providers FROM runtime_config WHERE is_active"
        )
    if row is None:
        return None
    return RuntimeState(int(row["revision"]), row["mode"], tuple(row["enabled_providers"]))


def _audit(
    conn: Connection,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO audit_events (actor_type, actor_id, action, target_type, target_id, details)"
        " VALUES ('operator', %s, %s, %s, %s, %s)",
        (actor, action, target_type, target_id, Jsonb(details)),
    )


def set_mode(
    conn: Connection,
    mode: Mode,
    *,
    actor: str,
    providers: tuple[str, ...] = ("email",),
    notes: str | None = None,
) -> int:
    if mode == "production":
        raise ControlError("production mode is Matthew's manual decision; not settable here")
    with transaction(conn, "ci_operator"):
        conn.execute("UPDATE runtime_config SET is_active = false WHERE is_active")
        row = one_required(
            conn,
            "INSERT INTO runtime_config (mode, enabled_providers, is_active, activated_at,"
            " activated_by, notes) VALUES (%s, %s, true, now(), %s, %s) RETURNING revision",
            (mode, list(providers), actor, notes),
        )
        _audit(conn, actor, "mode_change", "runtime_config", str(row["revision"]), {"mode": mode})
    return int(row["revision"])


def set_scope_approval(
    conn: Connection, scope_id: str, *, approved: bool, actor: str, backfill_since: datetime | None
) -> None:
    """Approve (or un-approve) a scope for the test dataset."""
    with transaction(conn, "ci_operator"):
        row = one(
            conn,
            "UPDATE source_scopes SET test_approved = %s,"
            " backfill_since = COALESCE(%s, backfill_since),"
            " approved_by = %s, approved_at = now() WHERE id = %s RETURNING id",
            (approved, backfill_since, actor, scope_id),
        )
        if row is None:
            raise ControlError("unknown scope")
        _audit(
            conn,
            actor,
            "scope_include" if approved else "scope_unapprove",
            "source_scope",
            scope_id,
            {"dataset": "test"},
        )


def add_exclusion(
    conn: Connection,
    kind: ExclusionKind,
    *,
    actor: str,
    reason_code: str,
    scope_kind: str | None = None,
    native_scope_id: str | None = None,
    pattern: str | None = None,
) -> str:
    if kind in ("participant_address", "participant_domain"):
        if not pattern:
            raise ControlError("a participant exclusion needs an address or domain")
        pattern = pattern.strip().lower().removeprefix("@")
        if kind == "participant_address" and "@" not in pattern:
            raise ControlError("an address exclusion needs a full address")
        if kind == "participant_domain" and "@" in pattern:
            raise ControlError("a domain exclusion takes a domain such as example.com")
    elif kind == "scope":
        if not (scope_kind and native_scope_id):
            raise ControlError("a scope exclusion needs the scope")
    else:
        raise ControlError("item exclusions are not exposed here")
    with transaction(conn, "ci_operator"):
        existing = one(
            conn,
            "SELECT id FROM exclusions WHERE revoked_at IS NULL AND exclusion_kind = %s"
            " AND pattern IS NOT DISTINCT FROM %s AND scope_kind IS NOT DISTINCT FROM %s"
            " AND native_scope_id IS NOT DISTINCT FROM %s",
            (kind, pattern, scope_kind, native_scope_id),
        )
        if existing is not None:
            return str(existing["id"])
        row = one_required(
            conn,
            "INSERT INTO exclusions (exclusion_kind, scope_kind, native_scope_id, pattern,"
            " reason_code, created_by) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (kind, scope_kind, native_scope_id, pattern, reason_code, actor),
        )
        _audit(
            conn,
            actor,
            "exclude",
            "exclusion",
            str(row["id"]),
            {"kind": kind, "reason_code": reason_code},
        )
    return str(row["id"])


def revoke_exclusion(conn: Connection, exclusion_id: str, *, actor: str) -> None:
    with transaction(conn, "ci_operator"):
        row = one(
            conn,
            "UPDATE exclusions SET revoked_at = now(), revoked_by = %s"
            " WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (actor, exclusion_id),
        )
        if row is None:
            raise ControlError("no active exclusion with that id")
        _audit(conn, actor, "exclusion_revoke", "exclusion", exclusion_id, {})


def active_exclusions(conn: Connection) -> list[dict[str, object]]:
    with transaction(conn, "ci_operator"):
        return many(
            conn,
            "SELECT id, exclusion_kind, scope_kind, native_scope_id, pattern, reason_code,"
            " created_by, created_at FROM exclusions WHERE revoked_at IS NULL ORDER BY created_at",
        )
