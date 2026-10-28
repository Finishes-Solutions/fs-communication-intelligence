"""Fireflies client interface and the real GraphQL client (``FIREFLIES_BACKEND=api``).

The API is GraphQL at ``https://api.fireflies.ai/graphql`` with a bearer API
key that belongs to one Fireflies user. Facts used here, from Fireflies'
published docs as summarized in docs/FIREFLIES_PIPELINE.md and NOT yet checked
against the Finishes account (the first real run is their first test):

- ``users`` lists the team's members.
- ``transcripts(fromDate, toDate, limit, skip, user_id)``: ``limit`` is at most
  50; ``user_id`` selects meetings that user organized or attended. Other
  integrators report pages silently capped at 50 and ``skip`` ordering that is
  not stable, so a full page splits the time window in half instead of paging.
- ``transcript(id)`` returns sentences (times in seconds), speakers,
  attendees and Fireflies' own summary. ``date`` is epoch milliseconds and
  ``duration`` is minutes.
- Business/Enterprise plans allow 60 requests per minute; this client paces
  itself below FIREFLIES_RATE_LIMIT_PER_MINUTE and retries 429 and 5xx.

Whether an admin key sees every member's meetings is the main open question:
capture lists the team-wide default and, unless turned off, each member by
``user_id``, then de-duplicates by transcript id. Errors carry status and
GraphQL error codes, never meeting content.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

PAGE_LIMIT = 50
MIN_SPLIT = timedelta(minutes=30)
RETRYABLE = {429, 500, 502, 503, 504}

USERS_QUERY = "query Users { users { user_id email name } }"
LIST_QUERY = """query Transcripts($fromDate: DateTime, $toDate: DateTime, $limit: Int,
  $skip: Int, $userId: String) {
  transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip,
              user_id: $userId) { id title date }
}"""
TRANSCRIPT_QUERY = """query Transcript($id: String!) {
  transcript(id: $id) {
    id title date duration organizer_email host_email participants transcript_url meeting_link
    meeting_attendees { displayName email name }
    speakers { id name }
    sentences { index speaker_id speaker_name text start_time end_time }
    summary { overview short_summary action_items keywords }
  }
}"""


class FirefliesError(RuntimeError):
    """A request failed. The message is a short code (status, GraphQL error code)."""


class TranscriptNotFound(FirefliesError):
    pass


@dataclass(frozen=True)
class FirefliesUser:
    user_id: str
    email: str
    name: str | None = None


@dataclass(frozen=True)
class TranscriptStub:
    id: str
    title: str | None
    date: datetime | None


class FirefliesClient(Protocol):
    def list_users(self) -> list[FirefliesUser]: ...

    def list_transcripts(
        self, since: datetime, until: datetime, user_id: str | None = None
    ) -> list[TranscriptStub]:
        """Every transcript dated in [since, until), de-duplicated by id."""
        ...

    def get_transcript(self, transcript_id: str) -> dict[str, Any]: ...


def epoch_ms_to_datetime(value: Any) -> datetime | None:
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def list_by_splitting(
    page: Callable[[datetime, datetime], list[TranscriptStub]],
    since: datetime,
    until: datetime,
) -> list[TranscriptStub]:
    """List a window. A full page may be capped, so split the window in half and list
    both halves; at the minimum window size accept the page (the caller logs the cap)."""
    found: dict[str, TranscriptStub] = {}
    pending = [(since, until)]
    while pending:
        start, end = pending.pop()
        stubs = page(start, end)
        if len(stubs) >= PAGE_LIMIT and end - start > MIN_SPLIT:
            middle = start + (end - start) / 2
            pending += [(start, middle), (middle, end)]
            continue
        for s in stubs:
            found.setdefault(s.id, s)
    return sorted(found.values(), key=lambda s: (s.date or since, s.id))


class _Pacer:
    """At most ``per_minute`` requests a minute from this process (thread-safe)."""

    def __init__(self, per_minute: int, sleep: Callable[[float], None]) -> None:
        self._gap = 60.0 / max(1, per_minute)
        self._next = 0.0
        self._lock = threading.Lock()
        self._sleep = sleep

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._gap
        if delay > 0:
            self._sleep(delay)


class HttpFirefliesClient:
    def __init__(
        self,
        api_key: str,
        *,
        url: str = "https://api.fireflies.ai/graphql",
        per_minute: int = 50,
        max_retries: int = 5,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise FirefliesError("no_api_key")
        self._key = api_key
        self._url = url
        self._http = client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep
        self._pacer = _Pacer(per_minute, sleep)

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self._pacer.wait()
            try:
                response = self._http.post(
                    self._url,
                    json={"query": query, "variables": variables},
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    self._sleep(min(2.0**attempt, 60.0))
                    continue
                raise FirefliesError(f"transport_{type(exc).__name__}") from exc
            if response.status_code in RETRYABLE and attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After", "")
                wait = float(retry_after) if retry_after.isdigit() else 2.0 ** (attempt + 1)
                self._sleep(min(wait, 120.0))
                continue
            try:
                body: dict[str, Any] = response.json()
            except ValueError as exc:
                raise FirefliesError(f"http_{response.status_code}_not_json") from exc
            errors = body.get("errors") or []
            if errors:
                codes = sorted(
                    {
                        str((e.get("extensions") or {}).get("code") or "graphql_error")
                        for e in errors
                        if isinstance(e, dict)
                    }
                )
                if any(c in ("object_not_found", "NOT_FOUND") for c in codes):
                    raise TranscriptNotFound(",".join(codes))
                if "too_many_requests" in codes and attempt < self._max_retries:
                    self._sleep(min(2.0 ** (attempt + 2), 120.0))
                    continue
                raise FirefliesError(f"http_{response.status_code}_{','.join(codes)}"[:120])
            if response.status_code >= 400:
                raise FirefliesError(f"http_{response.status_code}")
            data: dict[str, Any] = body.get("data") or {}
            return data
        raise FirefliesError("retries_exhausted")

    def list_users(self) -> list[FirefliesUser]:
        rows = self._post(USERS_QUERY, {}).get("users") or []
        return [
            FirefliesUser(str(u["user_id"]), str(u["email"]).strip().lower(), u.get("name"))
            for u in rows
            if u.get("user_id") and u.get("email")
        ]

    def list_transcripts(
        self, since: datetime, until: datetime, user_id: str | None = None
    ) -> list[TranscriptStub]:
        def page(start: datetime, end: datetime) -> list[TranscriptStub]:
            variables: dict[str, Any] = {
                "fromDate": _iso(start),
                "toDate": _iso(end),
                "limit": PAGE_LIMIT,
                "skip": 0,
            }
            if user_id:
                variables["userId"] = user_id
            rows = self._post(LIST_QUERY, variables).get("transcripts") or []
            return [
                TranscriptStub(str(r["id"]), r.get("title"), epoch_ms_to_datetime(r.get("date")))
                for r in rows
                if r.get("id")
            ]

        return list_by_splitting(page, since, until)

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        transcript = self._post(TRANSCRIPT_QUERY, {"id": transcript_id}).get("transcript")
        if not transcript:
            raise TranscriptNotFound("empty")
        return dict(transcript)
