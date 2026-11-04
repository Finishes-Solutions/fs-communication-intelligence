"""Fixture Fireflies client: serves synthetic meetings from ``tests/fixtures/fireflies``.

Layout:

- ``team.json``: the team's users (``user_id``, ``email``, ``name``, ``is_admin``,
  ``seed``: approve or leave) and which one owns the API key (``key_user``);
- ``transcripts/*.json``: one transcript each, shaped like the ``transcript`` query's
  answer, plus a ``fixture`` block: ``expect`` (what capture should do) and an
  optional ``after`` (a later revision: new title, extra sentences, a summary, or
  ``deleted``) applied by ``advance()``.

The client starts at round 1. Tests can inject one 429 with ``fail_next_with_429()``.
Only synthetic data belongs in these fixtures.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ci_fireflies.api import PAGE_SIZE, FirefliesUser, Throttled, TranscriptNotFound, TranscriptRef
from ci_fireflies.graphql_http import ref_from_listing

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fireflies"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TeamUser(_Model):
    user_id: str
    email: str
    name: str
    is_admin: bool = False
    seed: str = "approve"  # approve | leave


class Team(_Model):
    account_ref: str
    key_user: str
    backfill_since: str
    users: list[TeamUser] = Field(default_factory=list)


@dataclass
class FixtureTranscript:
    id: str
    rounds: list[dict[str, Any]]  # the transcript as seen in round 1, 2, ...
    expect: dict[str, Any] = field(default_factory=dict)

    def at(self, round_no: int) -> dict[str, Any] | None:
        data = self.rounds[min(round_no, len(self.rounds)) - 1]
        return None if data.get("deleted") else data


def _apply_after(base: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    nxt = copy.deepcopy(base)
    if after.get("deleted"):
        return {"deleted": True}
    for key in ("title", "summary", "meeting_attendees", "participants"):
        if key in after:
            nxt[key] = after[key]
    if "sentences_append" in after:
        start = len(nxt.get("sentences") or [])
        for i, s in enumerate(after["sentences_append"]):
            nxt.setdefault("sentences", []).append({"index": start + i, **s})
    return nxt


def load_team(directory: Path | None = None) -> Team:
    return Team.model_validate_json((Path(directory or FIXTURES_DIR) / "team.json").read_text())


def load_transcripts(directory: Path | None = None) -> list[FixtureTranscript]:
    out = []
    for path in sorted((Path(directory or FIXTURES_DIR) / "transcripts").glob("*.json")):
        raw = json.loads(path.read_text())
        meta = raw.pop("fixture", {})
        rounds = [raw]
        if meta.get("after"):
            rounds.append(_apply_after(raw, meta["after"]))
        out.append(FixtureTranscript(str(raw["id"]), rounds, dict(meta.get("expect") or {})))
    return out


class FixtureFirefliesClient:
    def __init__(self, team: Team, transcripts: list[FixtureTranscript]) -> None:
        self.team = team
        self.transcripts = {t.id: t for t in transcripts}
        self.round = 1
        self._fail_429 = False
        self.calls: list[str] = []  # counts only; for tests

    @classmethod
    def from_directory(cls, directory: Path | None = None) -> FixtureFirefliesClient:
        return cls(load_team(directory), load_transcripts(directory))

    def advance(self) -> None:
        self.round += 1

    def fail_next_with_429(self) -> None:
        self._fail_429 = True

    def _maybe_fail(self) -> None:
        if self._fail_429:
            self._fail_429 = False
            raise Throttled(1.0)

    def _users(self) -> list[FirefliesUser]:
        return [
            FirefliesUser(u.user_id, u.email.lower(), u.name, u.is_admin) for u in self.team.users
        ]

    # -- FirefliesClient ---------------------------------------------------------

    def whoami(self) -> FirefliesUser:
        self.calls.append("whoami")
        return next(u for u in self._users() if u.user_id == self.team.key_user)

    def list_users(self) -> list[FirefliesUser]:
        self.calls.append("users")
        return self._users()

    def list_transcripts(
        self, since: datetime, until: datetime, *, skip: int = 0
    ) -> list[TranscriptRef]:
        self.calls.append("transcripts")
        self._maybe_fail()
        refs = []
        for t in self.transcripts.values():
            data = t.at(self.round)
            if data is None:
                continue
            ref = ref_from_listing(data)
            if since <= ref.occurred_at < until:
                refs.append(ref)
        refs.sort(key=lambda r: r.occurred_at, reverse=True)
        return refs[skip : skip + PAGE_SIZE]

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        self.calls.append("transcript")
        self._maybe_fail()
        t = self.transcripts.get(transcript_id)
        data = t.at(self.round) if t else None
        if data is None:
            raise TranscriptNotFound(transcript_id)
        return copy.deepcopy(data)


def fixture_now() -> datetime:
    """A clock after every fixture meeting, so windows ending 'now' include them."""
    return datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
