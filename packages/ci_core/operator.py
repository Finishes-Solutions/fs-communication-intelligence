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


def approve_all_enrolled(conn: Connection, dataset: Dataset, actor: str) -> int:
    """Approve every reachable (``enrolled``) mailbox not yet approved; exclusions still win."""
    column = {"test": "test_approved", "production": "production_approved"}[dataset]
    rows = conn.execute(
        "SELECT m.primary_smtp_address AS address FROM source_scopes s"  # noqa: S608
        f" JOIN email_mailboxes m ON m.scope_id = s.id WHERE s.scope_state = 'enrolled'"
        f" AND NOT s.{column} ORDER BY 1"
    ).fetchall()
    for r in rows:
        approve_mailbox(conn, r["address"], dataset, actor)
    return len(rows)


_KEY = re.compile(r"^[a-z0-9_]{2,40}$")


def list_people(conn: Connection) -> list[dict[str, Any]]:
    """Profiles for step 2 of action items (ci_processing.routing)."""
    return conn.execute(
        "SELECT key, display_name, addresses, role, not_to_confuse, enabled, updated_by,"
        " updated_at FROM action_people ORDER BY key"
    ).fetchall()


def set_person(
    conn: Connection,
    actor: str,
    key: str,
    *,
    display_name: str,
    addresses: list[str],
    role: str = "",
    not_to_confuse: str = "",
    enabled: bool = True,
) -> None:
    """Create or replace a profile. Changing it makes processing re-ask the model."""
    if not _KEY.match(key):
        raise OperatorError("key must look like chuck_sr (a-z, 0-9, _)")
    cleaned = sorted({a.strip().lower() for a in addresses if a.strip()})
    if not cleaned or not all(_ADDRESS.match(a) for a in cleaned):
        raise OperatorError("give at least one valid email address")
    if not display_name.strip():
        raise OperatorError("give the person's name as people write it, e.g. Chuck Sr.")
    for text, limit in ((role, 500), (not_to_confuse, 1000)):
        if len(text) > limit:
            raise OperatorError(f"keep role and 'not to confuse with' under {limit} characters")
    conn.execute(
        "INSERT INTO action_people (key, display_name, addresses, role, not_to_confuse,"
        " enabled, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (key) DO UPDATE SET display_name = EXCLUDED.display_name,"
        " addresses = EXCLUDED.addresses, role = EXCLUDED.role,"
        " not_to_confuse = EXCLUDED.not_to_confuse, enabled = EXCLUDED.enabled,"
        " updated_by = EXCLUDED.updated_by, updated_at = now()",
        (key, display_name.strip(), cleaned, role.strip(), not_to_confuse.strip(), enabled, actor),
    )
    audit(
        conn,
        actor,
        "person_profile_set",
        "action_people",
        key,
        {"addresses": len(cleaned), "enabled": enabled},
    )


NAME_KINDS = ("property", "entity")


def list_names(conn: Connection) -> list[dict[str, Any]]:
    """Canonical property/project and legal-entity names (shown to the classifier)."""
    return conn.execute(
        "SELECT kind, canonical, aliases, created_by, created_at FROM known_names"
        " ORDER BY kind, lower(canonical)"
    ).fetchall()


def add_name(
    conn: Connection, actor: str, kind: str, canonical: str, aliases: list[str] | None = None
) -> None:
    """Add a canonical name, or replace the aliases of an existing one (same kind and name)."""
    if kind not in NAME_KINDS:
        raise OperatorError("kind must be property or entity")
    name = " ".join(canonical.split())
    if not 0 < len(name) <= 120:
        raise OperatorError("give a name of 1 to 120 characters")
    cleaned = sorted(
        {" ".join(a.split()) for a in aliases or [] if a.strip()} - {name}, key=str.casefold
    )
    if any(len(a) > 120 for a in cleaned):
        raise OperatorError("keep each alias under 120 characters")
    conn.execute(
        "INSERT INTO known_names (kind, canonical, aliases, created_by) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (kind, lower(canonical)) DO UPDATE SET aliases = EXCLUDED.aliases",
        (kind, name, cleaned, actor),
    )
    audit(
        conn, actor, "known_name_set", "known_names", None, {"kind": kind, "aliases": len(cleaned)}
    )


def remove_name(conn: Connection, actor: str, kind: str, canonical: str) -> None:
    removed = conn.execute(
        "DELETE FROM known_names WHERE kind = %s AND lower(canonical) = lower(%s) RETURNING id",
        (kind, " ".join(canonical.split())),
    ).fetchall()
    if not removed:
        raise OperatorError(f"no {kind} named {canonical!r}")
    audit(conn, actor, "known_name_removed", "known_names", str(removed[0]["id"]), {"kind": kind})


# ----- knowledge base corrections (migration 0016) ---------------------------
# The base is built without approval; these let an operator correct it. They
# change how entries are shown and counted, never the stored mentions, so each
# one can be undone.


def _entity(conn: Connection, entity_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, kind, name, name_override, merged_into, hidden FROM kg_entities WHERE id = %s",
        (entity_id,),
    ).fetchone()
    if row is None:
        raise OperatorError(f"no knowledge entry {entity_id}")
    return dict(row)


def knowledge_entry(conn: Connection, entity_id: int) -> dict[str, Any]:
    """An entry's correction state and the entries merged into it (operators only)."""
    entry = _entity(conn, entity_id)
    entry["merged_from"] = conn.execute(
        "SELECT id, name FROM kg_entities WHERE merged_into = %s ORDER BY id", (entity_id,)
    ).fetchall()
    return entry


def hidden_knowledge(conn: Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT id, kind, coalesce(name_override, name) AS name, updated_by, updated_at"
        " FROM kg_entities WHERE hidden ORDER BY updated_at DESC NULLS LAST, id"
    ).fetchall()


def _touch(
    conn: Connection, actor: str, entity_ids: list[int], changes: str, values: tuple[Any, ...]
) -> None:
    conn.execute(
        f"UPDATE kg_entities SET {changes}, updated_by = %s, updated_at = now()"  # noqa: S608
        " WHERE id = ANY(%s)",
        (*values, actor, entity_ids),
    )


def merge_knowledge(conn: Connection, actor: str, source_id: int, target_id: int) -> None:
    """Count ``source`` (and anything already merged into it) as ``target`` from now on.
    Both must be the same kind; a target that is itself merged is followed to its root."""
    source, target = _entity(conn, source_id), _entity(conn, target_id)
    if target["merged_into"] is not None:
        target = _entity(conn, target["merged_into"])
    if source["kind"] != target["kind"]:
        raise OperatorError(f"cannot merge a {source['kind']} into a {target['kind']}")
    if source["id"] == target["id"] or source["merged_into"] == target["id"]:
        raise OperatorError("those entries are already one")
    moved = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM kg_entities WHERE id = %s OR merged_into = %s", (source["id"],) * 2
        ).fetchall()
    ]
    _touch(conn, actor, moved, "merged_into = %s", (target["id"],))
    audit(
        conn,
        actor,
        "knowledge_merge",
        "kg_entities",
        str(target["id"]),
        {"source": source["id"], "entries": len(moved), "kind": source["kind"]},
    )


def unmerge_knowledge(conn: Connection, actor: str, entity_id: int) -> None:
    entry = _entity(conn, entity_id)
    if entry["merged_into"] is None:
        raise OperatorError(f"knowledge entry {entity_id} is not merged")
    _touch(conn, actor, [entity_id], "merged_into = NULL", ())
    audit(
        conn,
        actor,
        "knowledge_unmerge",
        "kg_entities",
        str(entity_id),
        {"was_merged_into": entry["merged_into"]},
    )


def hide_knowledge(conn: Connection, actor: str, entity_id: int, *, hidden: bool = True) -> None:
    """Hide an entry (and with it every fact naming it) from every reader, or show it again."""
    entry = _entity(conn, entity_id)
    _touch(conn, actor, [entity_id], "hidden = %s", (hidden,))
    audit(
        conn,
        actor,
        "knowledge_hide" if hidden else "knowledge_unhide",
        "kg_entities",
        str(entity_id),
        {"kind": entry["kind"]},
    )


def rename_knowledge(conn: Connection, actor: str, entity_id: int, name: str | None) -> None:
    """Set the name readers see (None or empty: back to the name emails use most)."""
    entry = _entity(conn, entity_id)
    cleaned = " ".join((name or "").split()) or None
    if cleaned is not None and len(cleaned) > 120:
        raise OperatorError("keep the name under 120 characters")
    _touch(conn, actor, [entity_id], "name_override = %s", (cleaned,))
    audit(
        conn,
        actor,
        "knowledge_rename",
        "kg_entities",
        str(entity_id),
        {"kind": entry["kind"], "cleared": cleaned is None},
    )
