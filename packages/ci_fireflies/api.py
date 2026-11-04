"""The Fireflies surface the meetings pipeline uses, as an interface.

Two implementations share it: ``FixtureFirefliesClient`` (``FIREFLIES_BACKEND=fixture``,
synthetic transcripts, no network) and ``HttpFirefliesClient`` (the GraphQL API with
the automation account's key). Capture depends only on this module.

Shapes come from the Fireflies GraphQL API as the connected account returned them on
2026-11-04 (the public schema page could not be fetched from this environment); the
query text lives in one place (``graphql_http.QUERIES``) so a field-name difference is
a one-line fix on the first real run (``ci fireflies check``):

- ``transcripts(fromDate, toDate, limit <= 50, skip)`` lists meetings newest first;
  ``date`` is epoch milliseconds, ``duration`` minutes;
- ``transcript(id)`` carries ``sentences`` (index, speaker_name, text, start_time and
  end_time in seconds), ``speakers``, ``participants`` (emails), ``meeting_attendees``
  (displayName, email), ``summary`` (overview, short_summary, action_items, keywords);
- ``users`` lists the team (user_id, email, name, is_admin) for an admin key;
- the API answers 429 when the plan's requests-per-minute limit is hit.

Return values carry content (titles, sentences), so never log them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


class FirefliesError(RuntimeError):
    pass


class Throttled(FirefliesError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"throttled; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class AccessDenied(FirefliesError):
    pass


class TranscriptNotFound(FirefliesError):
    pass


@dataclass(frozen=True)
class FirefliesUser:
    user_id: str  # source_scopes.native_scope_id
    email: str  # lowercase
    name: str
    is_admin: bool = False


@dataclass(frozen=True)
class TranscriptRef:
    """One listing row: enough to decide whether to fetch the transcript."""

    id: str
    occurred_at: datetime  # UTC
    duration_minutes: float
    organizer_email: str | None


PAGE_SIZE = 50  # the API's maximum ``limit``


class FirefliesClient(Protocol):
    def whoami(self) -> FirefliesUser: ...

    def list_users(self) -> list[FirefliesUser]: ...

    def list_transcripts(
        self, since: datetime, until: datetime, *, skip: int = 0
    ) -> list[TranscriptRef]:
        """One page (at most PAGE_SIZE) of meetings in [since, until), newest first."""
        ...

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        """The full transcript as the API returns it (content: never log it)."""
        ...
