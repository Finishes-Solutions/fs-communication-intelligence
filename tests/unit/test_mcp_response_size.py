"""Tool results stay short enough for agent platforms (Copilot set aside a long
list_actionable result and could not read it); trimmed lists say how to get the rest."""

import asyncio
import inspect
import json
import re
import threading
import time

import anyio.to_thread
import pytest

from ci_retrieval.mcp_server import MAX_RESPONSE_CHARS, fit, page


def emails(n: int, prefix: str = "e") -> list[dict[str, str]]:
    return [
        {
            "communication_id": f"{prefix}{i}",
            "subject": "Lien notice " * 10,
            "summary_ai": "x" * 400,
        }
        for i in range(n)
    ]


def test_short_results_are_untouched() -> None:
    data = {"note": "n", "actions": emails(3)}
    assert fit(data) == data


def test_long_lists_are_trimmed_from_the_end_with_paging_hints() -> None:
    data = {"note": "n", "actions": emails(120, "a"), "possible": [], "risks": emails(40, "r")}
    out = fit(data)
    assert len(json.dumps(out, separators=(",", ":"))) <= MAX_RESPONSE_CHARS
    # The longest list loses most; what stays is the newest (the first) of each.
    assert out["actions"] == data["actions"][: len(out["actions"])]
    assert out["risks"] == data["risks"][: len(out["risks"])]
    trimmed = out["trimmed"]
    assert trimmed["actions"]["left_out"] == 120 - len(out["actions"])
    assert trimmed["actions"]["next_offset"] == len(out["actions"])
    assert "possible" not in trimmed and "offset" in out["trimmed_note"]


def test_paging_skips_what_was_shown() -> None:
    data = {"actions": emails(30, "a"), "risks": emails(5, "r"), "note": "n"}
    second = page(data, ("actions", "risks"), offset=10, limit=10)
    assert [e["communication_id"] for e in second["actions"]] == [f"a{i}" for i in range(10, 20)]
    assert second["risks"] == [] and second["offset"] == 10
    later = fit({**page(data | {"actions": emails(200, "a")}, ("actions",), 50, 100)})
    assert later["trimmed"]["actions"]["next_offset"] == 50 + len(later["actions"])


def test_a_statement_stopped_for_time_is_answered_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The tool tells the agent the request was too large instead of leaving it to
    wait for its own timeout; the log has the tool's name and time, no arguments.
    The tool runs in a worker thread and the line is logged on the event loop,
    where the Azure Functions worker knows the invocation (AC-1.1, AC-1.2)."""
    from psycopg.errors import QueryCanceled

    from ci_retrieval.budget import ToolBudgetSpent
    from ci_retrieval.mcp_server import TOO_SLOW, _guarded

    ran_on: list[int] = []

    def email_stats(group_by: str, mailbox: str = "") -> dict[str, object]:
        ran_on.append(threading.get_ident())
        if group_by == "slow":
            raise QueryCanceled("canceling statement due to statement timeout")
        if group_by == "spent":
            raise ToolBudgetSpent
        if group_by == "broken":
            raise RuntimeError("boom")
        return {"groups": []}

    tool = _guarded(email_stats)

    async def scenario() -> None:
        assert await tool("month") == {"groups": []}
        assert await tool("slow", mailbox="private words") == TOO_SLOW
        assert await tool("spent", mailbox="private words") == TOO_SLOW
        with pytest.raises(RuntimeError):
            await tool("broken", mailbox="private words")

    with caplog.at_level("INFO", logger="ci_retrieval.mcp_server"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records]
    assert [line.split(" in ")[0] for line in lines] == [
        "mcp tool email_stats ok",
        "mcp tool email_stats too_slow",
        "mcp tool email_stats too_slow",
        "mcp tool email_stats error",
    ]
    assert all(re.fullmatch(r"mcp tool email_stats \w+ in \d+ ms", line) for line in lines)
    assert "private words" not in caplog.text
    here = threading.get_ident()
    assert len(ran_on) == 4 and here not in ran_on  # the tool body: a worker thread
    assert {r.thread for r in caplog.records} == {here}  # the log line: the event loop
    # The tool keeps its name and arguments (its input schema is built from them).
    assert tool.__name__ == "email_stats"
    assert list(inspect.signature(tool).parameters) == ["group_by", "mailbox"]


def test_a_tool_that_stalls_outside_the_database_is_answered_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An embedding call, a connection or a file fetch that hangs has no statement
    to stop: the answer is sent at the ceiling and the thread is left behind (AC-1.5)."""
    from ci_retrieval import mcp_server

    monkeypatch.setattr(mcp_server, "TOOL_CEILING_SECONDS", 0.05)
    finished = threading.Event()

    def get_file(file_id: str) -> dict[str, object]:
        time.sleep(0.3)
        finished.set()
        return {"found": True}

    async def scenario() -> tuple[object, float]:
        started = time.monotonic()
        answer = await mcp_server._guarded(get_file)("private words")
        return answer, time.monotonic() - started

    with caplog.at_level("INFO", logger="ci_retrieval.mcp_server"):
        answer, took = asyncio.run(scenario())
    assert answer == mcp_server.TOO_SLOW and took < 0.2
    assert not finished.is_set()  # answered while the tool was still working
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith("mcp tool get_file too_slow in ")
    assert finished.wait(2)  # the abandoned thread ends on its own


def test_lines_logged_inside_a_tool_carry_the_requests_invocation() -> None:
    """The Azure Functions worker reads the invocation of a thread's log line from
    its thread-local storage: it is set while the tool runs and put back after,
    because threads are reused (AC-1.6, without the HTTP app)."""
    from ci_retrieval import mcp_server

    storage = threading.local()
    seen: list[tuple[int, object]] = []

    def search_email(query: str) -> dict[str, object]:
        seen.append((threading.get_ident(), getattr(storage, "invocation_id", None)))
        return {"results": []}

    def after() -> tuple[int, object]:
        return threading.get_ident(), getattr(storage, "invocation_id", None)

    async def scenario() -> list[tuple[int, object]]:
        tool = mcp_server._guarded(search_email)
        mcp_server._invocation.set((storage, "invocation-1"))
        await tool("q")
        probes = [await anyio.to_thread.run_sync(after)]
        mcp_server._invocation.set(None)
        await tool("q")  # no invocation (stdio, tests): nothing is set
        return probes

    probes = asyncio.run(scenario())
    (thread, inside), (thread_2, outside) = seen
    assert inside == "invocation-1" and outside is None
    assert probes == [(thread, None)]  # the same thread, and nothing left on it
    assert thread_2 == thread
