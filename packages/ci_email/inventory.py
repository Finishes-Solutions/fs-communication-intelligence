"""Mailbox inventory: record what Graph shows for every mailbox (CLAUDE.md section 6.1).

Inventory records observed state only. It never approves a mailbox; approval
flags and exclusions are operator decisions. States written here:

- ``enrolled``: reachable, a user or shared mailbox, no active scope exclusion;
- ``excluded_by_rule``: an active scope exclusion exists (exclusions always
  win), or the recipient type is out of scope (room, equipment, other);
- ``no_exchange_access``: Graph answered 403 (outside the Exchange RBAC scope);
- ``no_mailbox``: Graph answered 404 (the user has no mailbox).

``not_approved`` is the column default before the first probe. Shared
mailboxes are expected to have disabled sign-in, so account state is never a
filter here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ci_core.db import Connection, required
from ci_core.operator import audit
from ci_email.graph import (
    GraphClient,
    GraphError,
    Mailbox,
    MailboxAccessDenied,
    MailboxNotFound,
)

POLICY_VERSION = "inventory-v1"
_RECIPIENT_TYPE = {
    "user": "UserMailbox",
    "shared": "SharedMailbox",
    "room": "RoomMailbox",
    "equipment": "EquipmentMailbox",
    "other": "Other",
    "unknown": "Other",
}


@dataclass(frozen=True)
class InventorySummary:
    states: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.states.values())


def _observe(conn: Connection, graph: GraphClient, mailbox: Mailbox) -> tuple[str, str | None]:
    excluded = conn.execute(
        "SELECT 1 FROM exclusions WHERE exclusion_kind = 'scope' AND revoked_at IS NULL"
        " AND scope_kind = 'mailbox' AND native_scope_id = %s",
        (mailbox.id,),
    ).fetchone()
    if excluded:
        return "excluded_by_rule", "operator_exclusion"
    if mailbox.purpose not in ("user", "shared", "unknown"):
        return "excluded_by_rule", f"recipient_type_{mailbox.purpose}"
    try:
        graph.list_folders(mailbox.id)
    except MailboxAccessDenied:
        return "no_exchange_access", "graph_403"
    except MailboxNotFound:
        return "no_mailbox", "graph_404"
    except GraphError as exc:
        return "no_exchange_access", f"graph_error_{type(exc).__name__.lower()}"
    return "enrolled", None


def sync_inventory(
    conn: Connection, graph: GraphClient, connection_id: str, corpus_id: str, actor: str
) -> InventorySummary:
    """Upsert one ``source_scopes`` + ``email_mailboxes`` row per mailbox Graph lists."""
    states: Counter[str] = Counter()
    for mailbox in graph.list_mailboxes():
        state, reason = _observe(conn, graph, mailbox)
        states[state] += 1
        row = required(
            conn.execute(
                """
            INSERT INTO source_scopes (connection_id, scope_kind, native_scope_id, display_label,
                                       corpus_id, scope_state, state_reason_code,
                                       state_checked_at, policy_version)
            VALUES (%s, 'mailbox', %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (connection_id, scope_kind, native_scope_id) DO UPDATE
               SET display_label = EXCLUDED.display_label, scope_state = EXCLUDED.scope_state,
                   state_reason_code = EXCLUDED.state_reason_code,
                   state_checked_at = EXCLUDED.state_checked_at
            RETURNING id::text
            """,
                (
                    connection_id,
                    mailbox.id,
                    mailbox.display_name,
                    corpus_id,
                    state,
                    reason,
                    POLICY_VERSION,
                ),
            ).fetchone()
        )
        conn.execute(
            """
            INSERT INTO email_mailboxes (scope_id, primary_smtp_address, recipient_type)
            VALUES (%s, %s, %s)
            ON CONFLICT (scope_id) DO UPDATE
               SET primary_smtp_address = EXCLUDED.primary_smtp_address,
                   recipient_type = EXCLUDED.recipient_type, last_inventory_at = now()
            """,
            (row["id"], mailbox.address.lower(), _RECIPIENT_TYPE[mailbox.purpose]),
        )
    summary = InventorySummary(dict(states))
    audit(
        conn,
        actor,
        "inventory_sync",
        "source_connection",
        connection_id,
        {"states": summary.states},
        actor_type="service",
    )
    return summary
