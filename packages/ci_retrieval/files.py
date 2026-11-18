"""The console's Files page: attachments of the mail a reader may see.

Matthew (2026-11-18): "a file explorer that default view is newest with ability
to search, have a view that groups files into folders by project, and company, etc."

Rows come from ``ci.published_files`` (migration 0032): attachments of published
emails, without signature images. As everywhere, the caller's grants and reader
limits (``_AUTH``) are applied before anything is listed or counted, and every
call is audited with ids and counts, never the search words.

A file attached to several emails, or held in several mailboxes, is listed once
(``file_key``: its SHA-256 when the original was read), with the newest email
it came with and how many emails carry it. Folders are made from those emails:
a file sent about two projects is in both project folders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ci_core.db import Connection
from ci_retrieval.identity import Principal
from ci_retrieval.search import _AUTH, Authorization, Filters, _audit, _like, _params, authorize

# Folder views: what a file is grouped by. Each is (key, label) SQL over the rows
# of published_files (alias f); a key that comes from a list (properties,
# entities) puts the file in one folder per name. Keys are lowercased so "Lot 12"
# and "LOT 12" share a folder; the label shown is the most common spelling.
GROUPS: dict[str, tuple[str, str]] = {
    "project": ("lower(btrim(x.v))", "btrim(x.v)"),
    "entity": ("lower(btrim(x.v))", "btrim(x.v)"),
    "company": (
        "coalesce(nullif(split_part(f.sender, '@', 2), ''), '')",
        "coalesce(nullif(split_part(f.sender, '@', 2), ''), '')",
    ),
    "sender": ("coalesce(f.sender, '')", "coalesce(f.sender, '')"),
    "mailbox": ("f.mailbox_address", "f.mailbox_address"),
    "type": (
        "coalesce(lower(substring(f.name from '\\.([A-Za-z0-9]{1,8})$')), '')",
        "coalesce(upper(substring(f.name from '\\.([A-Za-z0-9]{1,8})$')), '')",
    ),
    "month": (
        "to_char(f.occurred_at AT TIME ZONE %(tz)s, 'YYYY-MM')",
        "to_char(f.occurred_at AT TIME ZONE %(tz)s, 'YYYY-MM')",
    ),
}
# The list each list-valued grouping reads (as x.v); an email without names is in
# the folder with the empty key ("not found in the email").
_LISTS = {
    "project": "unnest(CASE WHEN cardinality(f.properties) = 0"
    " THEN ARRAY[''] ELSE f.properties END)",
    "entity": "unnest(CASE WHEN cardinality(f.entities) = 0 THEN ARRAY[''] ELSE f.entities END)",
}


def _unnest(group_by: str) -> str:
    return f" CROSS JOIN LATERAL {_LISTS[group_by]} AS x(v)" if group_by in _LISTS else ""


def _in_folder(group_by: str) -> str:
    """WHERE for the rows (alias f) of one folder, its key in %(group)s."""
    key, _ = GROUPS[group_by]
    if group_by in _LISTS:
        return f"WHERE EXISTS (SELECT 1 FROM {_LISTS[group_by]} AS x(v) WHERE {key} = %(group)s)"  # noqa: S608
    return f"WHERE {key} = %(group)s"


def _contains(group_by: str) -> str:
    """Condition (alias f): the grouping's value contains the text %(c_<group_by>)s
    (case-insensitive, already escaped for LIKE). For agents, who know a name, not
    a folder's exact key."""
    key, _ = GROUPS[group_by]
    match = f"{key} ILIKE '%%' || %(c_{group_by})s || '%%'"
    if group_by in _LISTS:
        return f"EXISTS (SELECT 1 FROM {_LISTS[group_by]} AS x(v) WHERE {match})"  # noqa: S608
    return match


SEARCH_IN = ("names", "text")
MAX_FOLDERS = 500
MAX_OFFSET = 5000


class FilesUnavailable(RuntimeError):
    """The database has no published_files yet (migration 0032 not applied)."""


def _ready(conn: Connection) -> None:
    row = conn.execute(
        "SELECT to_regprocedure('ci.published_files(timestamptz, timestamptz, text)')"
        " IS NOT NULL AS ok"
    ).fetchone()
    if not (row and row["ok"]):
        raise FilesUnavailable("the Files page needs database migration 0032")


def _base(search_in: str) -> str:
    """Authorized attachment rows of the period that match the search (alias f).
    By name: the file name, the email's subject or the sender contains the words.
    By text: the file's extracted text matches them (full text, in the function)."""
    names = (
        "AND (%(like)s::text IS NULL OR f.name ILIKE '%%' || %(like)s || '%%'"
        " OR f.title ILIKE '%%' || %(like)s || '%%'"
        " OR f.sender ILIKE '%%' || %(like)s || '%%')"
        if search_in == "names"
        else ""
    )
    return f"""
        SELECT f.* FROM published_files(%(since)s::timestamptz, %(until)s::timestamptz,
                                        %(text)s::text) f
        WHERE {_AUTH.format(a="f")} {names}"""  # noqa: S608 - fragments are constants


def _params_for(
    auth: Authorization,
    since: datetime | None,
    until: datetime | None,
    q: str | None,
    search_in: str,
) -> dict[str, Any]:
    if search_in not in SEARCH_IN:
        raise ValueError("search in names or text")
    words = (q or "").strip() or None
    return _params(auth, Filters(since=since, until=until)) | {
        "like": _like(words) if search_in == "names" else None,
        "text": words if search_in == "text" else None,
    }


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_key": r["file_key"],
        "name": r["name"],
        "content_type": r["content_type"],
        "size_bytes": int(r["size_bytes"] or 0),
        "file_id": r["sha256"] if r["original_stored"] else None,
        "attachment_id": r["attachment_id"],
        "extraction_status": r["extraction_status"],
        "emails": int(r["emails"]),
        "communication_id": str(r["communication_id"]),
        "subject": r["title"],
        "sender": r["sender"],
        "mailbox": r["mailbox_address"],
        "received_at": r["occurred_at"].isoformat(),
        "properties_ai": list(r["properties"] or []),
        "entities_ai": list(r["entities"] or []),
    }


def list_files(
    conn: Connection,
    principal: Principal,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    search_in: str = "names",
    group_by: str | None = None,
    group: str | None = None,
    tz: str = "America/Chicago",
    limit: int = 50,
    offset: int = 0,
    contains: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Files, newest first, one row per file; ``group_by``/``group`` keeps the files
    of one folder, ``contains`` (grouping -> text) the files whose project, company,
    sender... contains that text. ``has_more`` says whether the next ``offset``
    returns more."""
    _ready(conn)
    if group_by is not None and group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    wanted = {k: v.strip() for k, v in (contains or {}).items() if v and v.strip()}
    for name in wanted:
        if name not in GROUPS:
            raise ValueError(f"contains takes {', '.join(GROUPS)}")
    limit, offset = max(1, min(limit, 100)), max(0, min(offset, MAX_OFFSET))
    auth = authorize(conn, principal)
    rows: list[dict[str, Any]] = []
    more = False
    if not auth.empty:
        params = _params_for(auth, since, until, q, search_in) | {"tz": tz, "group": group}
        conditions = [_in_folder(group_by).removeprefix("WHERE ")] if group_by else []
        for name, text in wanted.items():
            conditions.append(_contains(name))
            params[f"c_{name}"] = _like(text.lower())
        within = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        found = conn.execute(
            f"""
            WITH f AS MATERIALIZED ({_base(search_in)}),
            g AS (SELECT * FROM f {within}),
            n AS (SELECT file_key,
                         count(DISTINCT coalesce(internet_message_id,
                                                 communication_id::text)) AS emails
                  FROM g GROUP BY file_key),
            newest AS (SELECT DISTINCT ON (g.file_key) g.*
                       FROM g ORDER BY g.file_key, g.occurred_at DESC, g.communication_id)
            SELECT newest.*, n.emails FROM newest JOIN n USING (file_key)
            ORDER BY newest.occurred_at DESC, newest.file_key
            LIMIT %(limit)s OFFSET %(offset)s
            """,  # noqa: S608 - fragments are constants; values are bound parameters
            params | {"limit": limit + 1, "offset": offset},
        ).fetchall()
        more = len(found) > limit
        rows = [_row(r) for r in found[:limit]]
    _audit(
        conn,
        principal,
        "list_files",
        {
            "result_ids": sorted({r["communication_id"] for r in rows}),
            "files": len(rows),
            "group_by": group_by,
            "search": search_in if q else None,
        },
    )
    conn.commit()
    return {"files": rows, "offset": offset, "has_more": more}


def file_folders(
    conn: Connection,
    principal: Principal,
    group_by: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = None,
    search_in: str = "names",
    tz: str = "America/Chicago",
) -> dict[str, Any]:
    """Folders for ``group_by``: each key with how many different files it holds
    and the newest email date. Largest first (months: newest first)."""
    _ready(conn)
    if group_by not in GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(GROUPS)}")
    key, label = GROUPS[group_by]
    auth = authorize(conn, principal)
    folders: list[dict[str, Any]] = []
    total = 0
    if not auth.empty:
        params = _params_for(auth, since, until, q, search_in) | {"tz": tz}
        order = "1 DESC" if group_by == "month" else "files DESC, label"
        found = conn.execute(
            f"""
            WITH f AS MATERIALIZED ({_base(search_in)})
            SELECT {key} AS key, mode() WITHIN GROUP (ORDER BY {label}) AS label,
                   count(DISTINCT f.file_key) AS files, max(f.occurred_at) AS last_at,
                   count(*) OVER () AS total
            FROM f {_unnest(group_by)}
            GROUP BY 1
            ORDER BY {order}
            LIMIT {MAX_FOLDERS}
            """,  # noqa: S608 - fragments are constants; values are bound parameters
            params,
        ).fetchall()
        total = int(found[0]["total"]) if found else 0
        folders = [
            {
                "key": r["key"],
                "label": r["label"],
                "files": int(r["files"]),
                "last_at": r["last_at"].isoformat(),
            }
            for r in found
        ]
    _audit(
        conn,
        principal,
        "file_folders",
        {"group_by": group_by, "folders": len(folders), "search": search_in if q else None},
    )
    conn.commit()
    return {"group_by": group_by, "folders": folders, "total_folders": total}
