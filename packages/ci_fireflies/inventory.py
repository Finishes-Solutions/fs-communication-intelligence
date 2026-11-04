"""Fireflies team inventory: one ``source_scopes`` row (kind ``fireflies_user``) per
team member, plus ``fireflies_users``. A member is a capture scope the way a mailbox
is: a meeting is captured through, and published under, the scopes of the team
members among its organizer, host, attendees and participants.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from ci_core.db import Connection, required
from ci_core.operator import OperatorError, audit
from ci_fireflies.api import FirefliesClient

POLICY_VERSION = "fireflies-scope-policy-v1"
CORPUS_KEY = "fireflies-meetings"
PROVIDER = "fireflies"


@dataclass(frozen=True)
class InventorySummary:
    states: dict[str, int]


def register_connection(conn: Connection, account_ref: str, actor: str) -> tuple[str, str]:
    """The Fireflies connection and the meetings corpus (idempotent). The corpus gets
    the same Reader.All grant the mail corpus has (D-M5: same roles as email)."""
    connection = required(
        conn.execute(
            "INSERT INTO source_connections (provider, account_ref, display_name, enabled)"
            " VALUES ('fireflies', %s, 'Fireflies.ai team', true)"
            " ON CONFLICT (provider, account_ref) DO UPDATE SET enabled = true RETURNING id::text",
            (account_ref,),
        ).fetchone()
    )["id"]
    corpus = required(
        conn.execute(
            "INSERT INTO corpora (corpus_key, description)"
            " VALUES (%s, 'Meetings recorded by Fireflies')"
            " ON CONFLICT (corpus_key) DO UPDATE SET corpus_key = EXCLUDED.corpus_key"
            " RETURNING id::text",
            (CORPUS_KEY,),
        ).fetchone()
    )["id"]
    if not conn.execute(
        "SELECT 1 FROM access_grants WHERE corpus_id = %s AND principal_type = 'entra_app_role'"
        " AND principal_id = 'Communications.Reader.All' AND revoked_at IS NULL",
        (corpus,),
    ).fetchone():
        conn.execute(
            "INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)"
            " VALUES (%s, 'entra_app_role', 'Communications.Reader.All', %s)",
            (corpus, actor),
        )
    audit(conn, actor, "fireflies_connection", "source_connection", connection, {})
    return connection, corpus


def connection_and_corpus(conn: Connection) -> tuple[str, str]:
    row = conn.execute(
        "SELECT c.id::text AS connection_id, k.id::text AS corpus_id"
        " FROM source_connections c, corpora k"
        " WHERE c.provider = 'fireflies' AND c.enabled AND k.corpus_key = %s"
        " ORDER BY c.created_at LIMIT 1",
        (CORPUS_KEY,),
    ).fetchone()
    if row is None:
        raise OperatorError("Fireflies not registered; run `ci fireflies init` (or `ci seed`)")
    return row["connection_id"], row["corpus_id"]


def sync_users(
    conn: Connection, client: FirefliesClient, connection_id: str, corpus_id: str, actor: str
) -> InventorySummary:
    """Upsert the team. Every member the key can list is ``enrolled``; approval for
    the dataset is a separate operator decision, as for mailboxes."""
    states: Counter[str] = Counter()
    for user in client.list_users():
        if not user.email:
            states["no_email"] += 1
            continue
        row = required(
            conn.execute(
                """
            INSERT INTO source_scopes (connection_id, scope_kind, native_scope_id, display_label,
                                       corpus_id, scope_state, state_checked_at, policy_version)
            VALUES (%s, 'fireflies_user', %s, %s, %s, 'enrolled', now(), %s)
            ON CONFLICT (connection_id, scope_kind, native_scope_id) DO UPDATE
               SET display_label = EXCLUDED.display_label, scope_state = 'enrolled',
                   state_checked_at = EXCLUDED.state_checked_at
            RETURNING id::text
            """,
                (connection_id, user.user_id, user.name or user.email, corpus_id, POLICY_VERSION),
            ).fetchone()
        )
        conn.execute(
            "INSERT INTO fireflies_users (scope_id, email, is_admin) VALUES (%s, %s, %s)"
            " ON CONFLICT (scope_id) DO UPDATE SET email = EXCLUDED.email,"
            " is_admin = EXCLUDED.is_admin, last_inventory_at = now()",
            (row["id"], user.email.lower(), user.is_admin),
        )
        states["enrolled"] += 1
    summary = InventorySummary(dict(states))
    audit(
        conn,
        actor,
        "fireflies_inventory_sync",
        "source_connection",
        connection_id,
        {"states": summary.states},
        actor_type="service",
    )
    return summary


def list_users(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT u.email, s.display_label AS name, u.is_admin, s.scope_state,
               s.test_approved, s.production_approved,
               EXISTS (SELECT 1 FROM exclusions e WHERE e.exclusion_kind = 'scope'
                       AND e.revoked_at IS NULL AND e.scope_kind = s.scope_kind
                       AND e.native_scope_id = s.native_scope_id) AS excluded
        FROM source_scopes s JOIN fireflies_users u ON u.scope_id = s.id
        WHERE s.scope_kind = 'fireflies_user' ORDER BY u.email
        """
    ).fetchall()


def _scope(conn: Connection, email: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT s.id::text AS scope_id, s.scope_kind, s.native_scope_id"
        " FROM source_scopes s JOIN fireflies_users u ON u.scope_id = s.id WHERE u.email = %s",
        (email.lower(),),
    ).fetchone()
    if row is None:
        raise OperatorError(f"no Fireflies team member {email}; run `ci fireflies inventory`")
    return dict(row)


def approve_user(
    conn: Connection, email: str, dataset: str, actor: str, *, revoke: bool = False
) -> None:
    column = {"test": "test_approved", "production": "production_approved"}[dataset]
    scope = _scope(conn, email)
    conn.execute(
        f"UPDATE source_scopes SET {column} = %s, approved_by = %s, approved_at = now()"  # noqa: S608
        " WHERE id = %s",
        (not revoke, actor, scope["scope_id"]),
    )
    audit(
        conn,
        actor,
        "scope_unapprove" if revoke else "scope_approve",
        "source_scope",
        scope["scope_id"],
        {"dataset": dataset, "source": PROVIDER},
    )


def approve_all(conn: Connection, dataset: str, actor: str) -> int:
    column = {"test": "test_approved", "production": "production_approved"}[dataset]
    rows = conn.execute(
        "SELECT u.email FROM source_scopes s JOIN fireflies_users u ON u.scope_id = s.id"  # noqa: S608
        f" WHERE s.scope_state = 'enrolled' AND NOT s.{column} ORDER BY 1"
    ).fetchall()
    for r in rows:
        approve_user(conn, r["email"], dataset, actor)
    return len(rows)


def exclude_user(
    conn: Connection, email: str, actor: str, *, reason: str | None = None, revoke: bool = False
) -> None:
    scope = _scope(conn, email)
    if revoke:
        updated = conn.execute(
            "UPDATE exclusions SET revoked_at = now(), revoked_by = %s"
            " WHERE exclusion_kind = 'scope' AND revoked_at IS NULL"
            " AND scope_kind = %s AND native_scope_id = %s RETURNING id::text",
            (actor, scope["scope_kind"], scope["native_scope_id"]),
        ).fetchall()
        if not updated:
            raise OperatorError(f"{email} has no active exclusion")
        audit(conn, actor, "scope_unexclude", "source_scope", scope["scope_id"], {})
        return
    if not reason:
        raise OperatorError("an exclusion needs --reason")
    row = conn.execute(
        "INSERT INTO exclusions (exclusion_kind, scope_kind, native_scope_id, reason_code,"
        " created_by) VALUES ('scope', %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id::text",
        (scope["scope_kind"], scope["native_scope_id"], reason, actor),
    ).fetchone()
    if row is None:
        raise OperatorError(f"{email} is already excluded")
    audit(conn, actor, "scope_exclude", "source_scope", scope["scope_id"], {"reason_code": reason})


def exclude_meeting(
    conn: Connection, transcript_id: str, actor: str, *, reason: str | None = None
) -> None:
    """Keep one meeting out (checked at capture and by the published views)."""
    if not reason:
        raise OperatorError("an exclusion needs --reason")
    connection_id, _ = connection_and_corpus(conn)
    row = conn.execute(
        "INSERT INTO exclusions (exclusion_kind, connection_id, native_id, reason_code, created_by)"
        " VALUES ('item', %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id::text",
        (connection_id, transcript_id, reason, actor),
    ).fetchone()
    if row is None:
        raise OperatorError("that meeting is already excluded")
    audit(conn, actor, "item_exclude", "communication", transcript_id, {"reason_code": reason})
