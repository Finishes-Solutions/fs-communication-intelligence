"""Fixture Fireflies client (``FIREFLIES_BACKEND=fixture``): serves the synthetic
workspace in ``tests/fixtures/fireflies/workspace.json`` through the same
interface as the real client.

To exercise per-user listing, the default listing (no ``user_id``) returns only
meetings organized by the key owner, which is one plausible behavior of a
non-admin key; listing by ``user_id`` returns meetings the user organized or
attended. ``fail_users`` makes listing for those user ids raise, to simulate
an API error for one member.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any

from ci_fireflies.client import (
    FirefliesError,
    FirefliesUser,
    TranscriptNotFound,
    TranscriptStub,
    epoch_ms_to_datetime,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fireflies"


class FixtureFirefliesClient:
    def __init__(self, workspace: dict[str, Any], fail_users: Collection[str] = ()) -> None:
        self.workspace = workspace
        self.fail_users = frozenset(fail_users)
        self.calls: list[str] = []

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> FixtureFirefliesClient:
        path = (directory or FIXTURE_DIR) / "workspace.json"
        return cls(json.loads(path.read_text()))

    def _transcripts(self) -> list[dict[str, Any]]:
        return list(self.workspace["transcripts"])

    def list_users(self) -> list[FirefliesUser]:
        self.calls.append("users")
        return [
            FirefliesUser(u["user_id"], u["email"].lower(), u.get("name"))
            for u in self.workspace["users"]
        ]

    def list_transcripts(
        self, since: datetime, until: datetime, user_id: str | None = None
    ) -> list[TranscriptStub]:
        self.calls.append(f"list:{user_id or '-'}")
        if user_id in self.fail_users:
            raise FirefliesError("http_500")
        if user_id is None:
            email = self.workspace["key_owner_email"]
        else:
            matches = [u["email"] for u in self.workspace["users"] if u["user_id"] == user_id]
            if not matches:
                return []
            email = matches[0]
        out = []
        for t in self._transcripts():
            when = epoch_ms_to_datetime(t["date"])
            if when is None or not since <= when < until:
                continue
            attended = email in (t.get("participants") or [])
            if t["organizer_email"] == email or (user_id is not None and attended):
                out.append(TranscriptStub(t["id"], t.get("title"), when))
        return sorted(out, key=lambda s: s.id)

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        self.calls.append(f"get:{transcript_id}")
        for t in self._transcripts():
            if t["id"] == transcript_id:
                return copy.deepcopy(t)
        raise TranscriptNotFound("object_not_found")
