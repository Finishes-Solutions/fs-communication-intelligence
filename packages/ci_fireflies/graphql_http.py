"""The real Fireflies client (``FIREFLIES_BACKEND=api``): GraphQL over HTTPS.

Authenticates with the automation account's API key (``FIREFLIES_API_KEY``, a Key
Vault reference in Azure, never in the repository). Requests are paced to the
plan's per-minute limit and retried on 429 with the server's ``Retry-After``.
Not yet run against the real account: ``ci fireflies check`` is its first test.
Errors carry codes and counts, never transcript content.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ci_fireflies.api import (
    PAGE_SIZE,
    AccessDenied,
    FirefliesError,
    FirefliesUser,
    Throttled,
    TranscriptNotFound,
    TranscriptRef,
)

DEFAULT_URL = "https://api.fireflies.ai/graphql"
RETRYABLE = {429, 500, 502, 503, 504}

QUERIES = {
    "whoami": "query { user { user_id email name is_admin } }",
    "users": "query { users { user_id email name is_admin } }",
    "transcripts": (
        "query Transcripts($fromDate: DateTime, $toDate: DateTime, $limit: Int, $skip: Int) {"
        " transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit, skip: $skip) {"
        " id date duration organizer_email } }"
    ),
    "transcript": (
        "query Transcript($id: String!) { transcript(id: $id) {"
        " id title date dateString duration organizer_email host_email participants"
        " transcript_url meeting_link"
        " meeting_attendees { displayName email }"
        " speakers { id name }"
        " sentences { index speaker_name speaker_id text start_time end_time }"
        " summary { overview short_summary action_items keywords }"
        " meeting_info { silent_meeting summary_status } } }"
    ),
}


def _user(row: dict[str, Any]) -> FirefliesUser:
    return FirefliesUser(
        str(row["user_id"]),
        str(row.get("email") or "").lower(),
        str(row.get("name") or ""),
        bool(row.get("is_admin")),
    )


def ref_from_listing(row: dict[str, Any]) -> TranscriptRef:
    date = row.get("date")
    occurred = (
        datetime.fromtimestamp(float(date) / 1000, tz=UTC)
        if isinstance(date, int | float)
        else datetime.fromisoformat(str(row.get("dateString") or date).replace("Z", "+00:00"))
    )
    organizer = row.get("organizer_email")
    return TranscriptRef(
        str(row["id"]),
        occurred,
        float(row.get("duration") or 0.0),
        str(organizer).lower() if organizer else None,
    )


class HttpFirefliesClient:
    def __init__(
        self,
        api_key: str,
        *,
        url: str = DEFAULT_URL,
        requests_per_minute: int = 60,
        max_retries: int = 5,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = api_key
        self._url = url
        self._interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._max_retries = max_retries
        self._http = client or httpx.Client(timeout=timeout)
        self._sleep = sleep
        self._clock = clock
        self._last = -1e9

    def close(self) -> None:
        self._http.close()

    def _pace(self) -> None:
        wait = self._interval - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _query(self, name: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            self._pace()
            response = self._http.post(
                self._url,
                json={"query": QUERIES[name], "variables": variables or {}},
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code in RETRYABLE:
                if attempt < self._max_retries:
                    wait = float(response.headers.get("Retry-After", 2 ** min(attempt, 5)))
                    self._sleep(min(wait, 120.0))
                    continue
                if response.status_code == 429:
                    raise Throttled(float(response.headers.get("Retry-After", 60)))
                raise FirefliesError(f"HTTP {response.status_code}")
            if response.status_code in (401, 403):
                raise AccessDenied(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                raise FirefliesError(f"HTTP {response.status_code}")
            body = response.json()
            errors = body.get("errors") or []
            if errors:
                code = str((errors[0].get("extensions") or {}).get("code") or "graphql_error")
                if code in ("too_many_requests", "rate_limit_exceeded"):
                    raise Throttled(60.0)
                if code in ("object_not_found", "not_found"):
                    raise TranscriptNotFound(code)
                if code in ("forbidden", "unauthorized", "paid_required"):
                    raise AccessDenied(code)
                raise FirefliesError(code)
            data = body.get("data")
            if not isinstance(data, dict):
                raise FirefliesError("no data")
            return data
        raise FirefliesError("retries exhausted")

    # -- FirefliesClient ---------------------------------------------------------

    def whoami(self) -> FirefliesUser:
        return _user(self._query("whoami")["user"])

    def list_users(self) -> list[FirefliesUser]:
        return [_user(u) for u in self._query("users")["users"] or []]

    def list_transcripts(
        self, since: datetime, until: datetime, *, skip: int = 0
    ) -> list[TranscriptRef]:
        rows = self._query(
            "transcripts",
            {
                "fromDate": since.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "toDate": until.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "limit": PAGE_SIZE,
                "skip": skip,
            },
        )["transcripts"]
        refs = [ref_from_listing(r) for r in rows or []]
        # The API treats the range as inclusive; keep [since, until) ourselves.
        return [r for r in refs if since <= r.occurred_at < until]

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        data = self._query("transcript", {"id": transcript_id})["transcript"]
        if not isinstance(data, dict):
            raise TranscriptNotFound(transcript_id)
        return data
