"""The active mode and capture scope, read from PostgreSQL. Both fail closed.

``runtime_config`` holds exactly one active revision (ADR-004/011). A mailbox
is eligible for capture only when the mode is not paused, the provider is
enabled, the connection is enabled, the scope is ``enrolled`` and approved for
the active dataset, and no active scope exclusion exists. Exclusions always
win. An empty eligible set refuses capture instead of meaning "everything".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ci_core.db import Connection

Mode = Literal["test", "production", "paused"]


class RuntimeConfigError(RuntimeError):
    """No usable runtime configuration: capture and publication must stop."""


class EmptyScopeError(RuntimeConfigError):
    """The active mode selects no mailboxes. Capture refuses to run."""


@dataclass(frozen=True)
class RuntimeConfig:
    revision: int
    mode: Mode
    enabled_providers: tuple[str, ...]

    @property
    def dataset(self) -> str | None:
        return None if self.mode == "paused" else self.mode


@dataclass(frozen=True)
class EligibleScope:
    scope_id: str
    connection_id: str
    native_scope_id: str
    address: str


def active_config(conn: Connection) -> RuntimeConfig:
    row = conn.execute(
        "SELECT revision, mode, enabled_providers FROM runtime_config WHERE is_active"
    ).fetchone()
    if row is None:
        raise RuntimeConfigError("no active runtime_config revision")
    return RuntimeConfig(row["revision"], row["mode"], tuple(row["enabled_providers"]))


def eligible_mailboxes(conn: Connection) -> list[EligibleScope]:
    """Mailboxes capture may read under the active revision ([] when paused)."""
    config = active_config(conn)
    if config.dataset is None or "email" not in config.enabled_providers:
        return []
    approved = "test_approved" if config.dataset == "test" else "production_approved"
    rows = conn.execute(
        f"""
        SELECT s.id::text AS scope_id, s.connection_id::text AS connection_id,
               s.native_scope_id, m.primary_smtp_address AS address
        FROM source_scopes s
        JOIN source_connections c ON c.id = s.connection_id AND c.enabled AND c.provider = 'email'
        JOIN email_mailboxes m ON m.scope_id = s.id
        WHERE s.scope_kind = 'mailbox' AND s.scope_state = 'enrolled' AND s.{approved}
          AND NOT EXISTS (SELECT 1 FROM exclusions e
                          WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL
                            AND e.scope_kind = s.scope_kind
                            AND e.native_scope_id = s.native_scope_id)
        ORDER BY m.primary_smtp_address
        """  # noqa: S608 - the column name is one of two literals above
    ).fetchall()
    return [EligibleScope(**row) for row in rows]


def require_capture_scope(conn: Connection) -> list[EligibleScope]:
    scopes = eligible_mailboxes(conn)
    if not scopes:
        raise EmptyScopeError(
            f"mode {active_config(conn).mode} selects no mailboxes; capture refuses to run"
        )
    return scopes
