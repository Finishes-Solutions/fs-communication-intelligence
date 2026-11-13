"""Opening a database connection retries a connection timeout, then gives up."""

from typing import Any

import psycopg
import pytest

from ci_core import db


def test_connection_timeouts_are_retried_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    pauses: list[float] = []
    sentinel = object()

    def fake_connect(conninfo: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        if len(calls) < 3:
            raise psycopg.errors.ConnectionTimeout("connection timeout expired")
        return sentinel

    monkeypatch.setenv("DATABASE_AUTH", "password")
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert db.connect(sleep=pauses.append) is sentinel
    assert len(calls) == 3 and pauses == [2.0, 5.0]
    assert calls[0]["connect_timeout"] == db.KEEPALIVE["connect_timeout"]


def test_gives_up_after_the_last_attempt_and_other_errors_are_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []

    def always_timeout(conninfo: str, **kwargs: Any) -> object:
        raise psycopg.errors.ConnectionTimeout("connection timeout expired")

    monkeypatch.setenv("DATABASE_AUTH", "password")
    monkeypatch.setattr(psycopg, "connect", always_timeout)
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        db.connect(sleep=pauses.append)
    assert pauses == [2.0, 5.0, 10.0]  # CONNECT_ATTEMPTS = 4

    def refused(conninfo: str, **kwargs: Any) -> object:
        raise psycopg.OperationalError("password authentication failed")

    monkeypatch.setattr(psycopg, "connect", refused)
    pauses.clear()
    with pytest.raises(psycopg.OperationalError, match="authentication"):
        db.connect(sleep=pauses.append)
    assert pauses == []  # a refusal is not a timeout: no retry
