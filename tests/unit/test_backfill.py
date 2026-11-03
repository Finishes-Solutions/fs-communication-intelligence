"""Backfill plans: how a long period is cut into windows, how a window is captured,
and how the Graph client asks for it. No database."""

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from ci_core.db import AppRole, Connection
from ci_core.operator import OperatorError
from ci_core.ops import BACKFILL_MAX_WINDOW_DAYS, BackfillWindow, local_midnight, plan_windows
from ci_email.capture import MAX_CAPTURE_HOURS, capture_window
from ci_email.graph import GraphClient
from ci_email.graph_http import HttpGraphClient

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class FakeCredential:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="test-token")  # noqa: S106


def test_windows_are_newest_first_and_the_oldest_is_the_remainder() -> None:
    since = local_midnight("2025-01-01")  # 2025-01-01 06:00 UTC (CST)
    windows = plan_windows(since, NOW, 7)
    assert windows[0][1] == NOW and windows[-1][0] == since
    assert all(end - start == timedelta(days=7) for start, end in windows[:-1])
    assert all(a_start == b_end for (a_start, _), (b_start, b_end) in itertools.pairwise(windows))
    assert len(windows) == 96  # 671 days and 6 hours / 7 days, rounded up
    assert BackfillWindow(1, "p", windows[0][0], windows[0][1], "pending").hours == 7 * 24


def test_windows_are_at_most_93_days_and_the_period_must_be_forward() -> None:
    assert BACKFILL_MAX_WINDOW_DAYS * 24 == MAX_CAPTURE_HOURS
    with pytest.raises(OperatorError, match="between 1 and 93"):
        plan_windows(NOW - timedelta(days=100), NOW, 94)
    with pytest.raises(OperatorError, match="after its start"):
        plan_windows(NOW, NOW, 7)
    with pytest.raises(OperatorError, match="YYYY-MM-DD"):
        local_midnight("01/01/2025")


def test_capture_window_takes_hours_or_an_explicit_window_never_both() -> None:
    def no_db(role: AppRole | None = None) -> Connection:
        raise AssertionError("must not connect")

    graph = cast(GraphClient, None)
    since = NOW - timedelta(days=7)
    with pytest.raises(ValueError, match="both since and until"):
        capture_window(
            no_db, graph, hours=24, include_sent=True, profile={}, since=since, until=NOW
        )
    with pytest.raises(ValueError, match="both since and until"):
        capture_window(no_db, graph, include_sent=True, profile={}, since=since)
    with pytest.raises(ValueError, match="at most 2232 hours"):
        long_ago = NOW - timedelta(days=94)
        capture_window(no_db, graph, include_sent=True, profile={}, since=long_ago, until=NOW)
    with pytest.raises(ValueError, match="--hours"):
        capture_window(no_db, graph, include_sent=True, profile={})


def test_graph_lists_a_window_lazily_with_both_bounds() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "skip" not in str(request.url):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "m1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/u/messages?skip=1",
                },
            )
        return httpx.Response(200, json={"value": [{"id": "m2"}]})

    client = HttpGraphClient(
        "t",
        "c",
        "unused.pem",
        credential=FakeCredential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    messages = client.list_messages(
        "u",
        received_since=datetime(2025, 1, 1, 6, tzinfo=UTC),
        received_until=datetime(2025, 1, 8, 6, tzinfo=UTC),
    )
    assert not seen  # nothing is fetched until the caller reads
    assert next(iter(messages))["id"] == "m1"
    assert len(seen) == 1  # the second page is not fetched yet
    assert seen[0].url.params["$filter"] == (
        "receivedDateTime ge 2025-01-01T06:00:00Z and receivedDateTime lt 2025-01-08T06:00:00Z"
    )
    assert [m["id"] for m in messages] == ["m2"]
