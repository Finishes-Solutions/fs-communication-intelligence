"""``ci seed``: set up the local database from the synthetic fixture tenant.

Creates the fixture connection, the ``email-enrolled`` corpus, a reader grant
for the ``Communications.Reader.All`` app role and (if none is active) a
``test`` mode revision; syncs the inventory from the fixture Graph client; then
applies the fixture's approvals and exclusions. Safe to run again. Messages
are not loaded here: capture reads them through the fixture Graph client.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ci_core.db import Connection, required
from ci_core.operator import approve_mailbox, audit, exclude_mailbox, set_mode
from ci_email.fixture import FixtureGraphClient
from ci_email.inventory import InventorySummary, sync_inventory

CORPUS_KEY = "email-enrolled"
READER_ALL_ROLE = "Communications.Reader.All"


@dataclass(frozen=True)
class SeedResult:
    connection_id: str
    corpus_id: str
    inventory: InventorySummary
    approved: list[str]
    excluded: list[str]


def seed(conn: Connection, actor: str, fixtures_dir: Path | None = None) -> SeedResult:
    graph = FixtureGraphClient.from_directory(fixtures_dir)
    tenant = graph.tenant
    conn_row = required(
        conn.execute(
            "INSERT INTO source_connections (provider, account_ref, display_name, enabled)"
            " VALUES ('email', %s, 'Fixture tenant (synthetic)', true)"
            " ON CONFLICT (provider, account_ref) DO UPDATE SET enabled = true RETURNING id::text",
            (tenant.tenant_id,),
        ).fetchone()
    )
    corpus_row = required(
        conn.execute(
            "INSERT INTO corpora (corpus_key, description)"
            " VALUES (%s, 'Mail from enrolled mailboxes')"
            " ON CONFLICT (corpus_key) DO UPDATE SET corpus_key = EXCLUDED.corpus_key"
            " RETURNING id::text",
            (CORPUS_KEY,),
        ).fetchone()
    )
    connection_id, corpus_id = conn_row["id"], corpus_row["id"]

    has_grant = conn.execute(
        "SELECT 1 FROM access_grants WHERE corpus_id = %s AND principal_type = 'entra_app_role'"
        " AND principal_id = %s AND revoked_at IS NULL",
        (corpus_id, READER_ALL_ROLE),
    ).fetchone()
    if not has_grant:
        conn.execute(
            "INSERT INTO access_grants (corpus_id, principal_type, principal_id, granted_by)"
            " VALUES (%s, 'entra_app_role', %s, %s)",
            (corpus_id, READER_ALL_ROLE, actor),
        )
    if not conn.execute("SELECT 1 FROM runtime_config WHERE is_active").fetchone():
        set_mode(conn, "test", actor, note="ci seed")

    sync_inventory(conn, graph, connection_id, corpus_id, actor)
    approved, excluded = [], []
    for mailbox in tenant.mailboxes:
        state = required(
            conn.execute(
                "SELECT s.test_approved, EXISTS (SELECT 1 FROM exclusions e"
                "   WHERE e.exclusion_kind = 'scope' AND e.revoked_at IS NULL"
                "     AND e.scope_kind = 'mailbox' AND e.native_scope_id = s.native_scope_id)"
                "   AS excluded"
                " FROM source_scopes s WHERE s.connection_id = %s AND s.native_scope_id = %s",
                (connection_id, mailbox.id),
            ).fetchone()
        )
        if mailbox.seed == "approve" and not state["test_approved"]:
            approve_mailbox(conn, mailbox.address, "test", actor)
            approved.append(mailbox.address)
        if mailbox.seed == "exclude" and not state["excluded"]:
            exclude_mailbox(conn, mailbox.address, actor, reason="fixture_excluded")
            excluded.append(mailbox.address)
    # Re-run so the exclusions above show as excluded_by_rule.
    summary = sync_inventory(conn, graph, connection_id, corpus_id, actor)
    audit(
        conn,
        actor,
        "seed",
        "source_connection",
        connection_id,
        {"approved": len(approved), "excluded": len(excluded)},
    )
    return SeedResult(connection_id, corpus_id, summary, approved, excluded)
