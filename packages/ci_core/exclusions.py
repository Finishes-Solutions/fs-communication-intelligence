"""Capture-time exclusion checks shared by every source.

Exclusions are checked before content is staged and again by the
``published_*`` views, so one added later still hides evidence already stored.
"""

from __future__ import annotations

from collections.abc import Iterable

from ci_core.db import Connection


def item_excluded(conn: Connection, connection_id: str, native_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM exclusions WHERE exclusion_kind = 'item' AND revoked_at IS NULL"
            " AND connection_id = %s AND native_id = %s",
            (connection_id, native_id),
        ).fetchone()
        is not None
    )


def participants_excluded(conn: Connection, addresses: Iterable[str]) -> bool:
    """True when any address, or its domain, has an active participant exclusion."""
    cleaned = sorted({a.strip().lower() for a in addresses if a and a.strip()})
    domains = sorted({a.split("@", 1)[1] for a in cleaned if "@" in a})
    row = conn.execute(
        "SELECT 1 FROM exclusions WHERE revoked_at IS NULL AND ("
        " (exclusion_kind = 'participant_address' AND pattern = ANY(%s)) OR"
        " (exclusion_kind = 'participant_domain' AND pattern = ANY(%s))) LIMIT 1",
        (cleaned, domains),
    ).fetchone()
    return row is not None
