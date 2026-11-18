"""Short-lived download links that work without signing in (migration 0033).

Matthew (2026-11-18): agents should fetch files and emails "without needing to use
my computer or browser". A link names one stored file, or one email exported as
Markdown or Word, and the reader it was made for; it is signed with the key in
``ci.file_link_key()`` and expires after ``Settings.file_link_minutes``.

Whoever holds a link can download that one thing until it expires: links are
bearer secrets, and they end up in AI platforms' chat histories. That is why they
are short-lived, name a single item, and are checked again at download against
the reader's grants and limits (``ci_admin.api`` ``/dl/<link>``), so revoking a
grant or adding a reader limit takes effect for links already handed out.

Format: ``base64url(json payload) "." base64url(HMAC-SHA256(payload))``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from ci_core.db import Connection
from ci_retrieval.identity import Principal

Kind = Literal["file", "email"]
EMAIL_FORMATS = ("md", "docx")
# The key is read from the database at most this often (rotation takes effect
# within this time).
KEY_SECONDS = 300.0

_key_cache: dict[str, tuple[float, bytes]] = {}
_key_lock = threading.Lock()


class LinksUnavailable(RuntimeError):
    """The database has no file link key yet (migration 0033 not applied)."""


@dataclass(frozen=True)
class Link:
    kind: Kind
    communication_id: str
    # The stored file's SHA-256 (kind "file") or the export format (kind "email").
    item: str
    principal: Principal
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def key(conn: Connection, *, max_age: float = KEY_SECONDS) -> bytes:
    """The signing key, cached for ``max_age`` seconds per database."""
    name = conn.info.dbname
    with _key_lock:
        cached = _key_cache.get(name)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
    row = conn.execute(
        "SELECT CASE WHEN to_regprocedure('ci.file_link_key()') IS NULL THEN NULL"
        " ELSE ci.file_link_key() END AS key"
    ).fetchone()
    if not row or not row["key"]:
        raise LinksUnavailable("download links need database migration 0033")
    value = bytes(row["key"])
    with _key_lock:
        _key_cache[name] = (time.monotonic(), value)
    return value


def sign(link: Link, secret: bytes) -> str:
    payload = json.dumps(
        {
            "k": link.kind,
            "c": link.communication_id,
            "i": link.item,
            "o": link.principal.oid,
            "u": link.principal.upn,
            "r": list(link.principal.roles),
            "x": link.expires_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    mac = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify(token: str, secret: bytes, *, now: float | None = None) -> Link | None:
    """The link a token names, or None if it is malformed, forged or expired."""
    try:
        body, mac = token.split(".", 1)
        payload = _unb64(body)
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(mac)):
            return None
        data: dict[str, Any] = json.loads(payload)
        link = Link(
            kind=data["k"],
            communication_id=str(data["c"]),
            item=str(data["i"]),
            principal=Principal(str(data["o"]), data.get("u"), tuple(data.get("r") or ())),
            expires_at=int(data["x"]),
        )
    except ValueError, KeyError, TypeError:
        return None
    if link.kind not in ("file", "email"):
        return None
    if link.kind == "email" and link.item not in EMAIL_FORMATS:
        return None
    if link.expires_at < (time.time() if now is None else now):
        return None
    return link


def make(
    conn: Connection,
    principal: Principal,
    kind: Kind,
    communication_id: str,
    item: str,
    minutes: float,
    *,
    now: float | None = None,
) -> tuple[str, int]:
    """A signed link token and its expiry (Unix seconds)."""
    expires = int((time.time() if now is None else now) + minutes * 60)
    link = Link(kind, communication_id, item, principal, expires)
    return sign(link, key(conn)), expires
