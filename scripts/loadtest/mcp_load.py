"""Load test for the hosted MCP endpoint: many simulated AI agents at once.

Matthew, 2026-11-18: "what would I need to scale this to if I have 100 agents
gathering info from here at once?" This measures it instead of guessing. Each
simulated agent calls a realistic mix of read-only tools (search, lists, one
email, action items, counts, risks, files), waits a random "thinking" time,
and repeats. At the end it prints, per tool, how many calls succeeded, failed
or were stopped as too slow, and how long they took (median, 95th and 99th
percentile, slowest).

It only reads. It prints tool names, counts, timings and error codes, never
email text. Every call is a real, audited call by the signed-in account, on the
live database: start small (10 agents) at a quiet time and watch the database
(CPU, connections) while it runs.

    uv run python scripts/loadtest/mcp_load.py --agents 10 --minutes 3
    uv run python scripts/loadtest/mcp_load.py --agents 100 --minutes 5 --yes

The token comes from --token, the MCP_TOKEN environment variable, or the
signed-in Azure CLI (needs scripts/azure/12-loadtest-cli-access.sh once).
"""

# Random choices pick test traffic, not secrets (S311); the Azure CLI is run with fixed
# arguments (S603/S607).
# ruff: noqa: S311, S603, S607

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

DEFAULT_URL = "https://func-fs-ci-srch-dev-1026.azurewebsites.net/mcp"
API_NAME = "FS Communications Search API"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
# Generic business words: what agents ask about, never real email content.
QUERIES = [
    "invoice", "change order", "failed inspection", "lien waiver", "draw request",
    "schedule delay", "permit", "certificate of insurance", "lease renewal", "punch list",
    "pay application", "retainage", "RFI response", "closing documents", "utility easement",
]  # fmt: skip
# How often each tool is called (weights). Searches and lists dominate, as for a
# typical agent; counts and the risk overview are the heavy ones.
MIX = {
    "search_email": 35,
    "list_email": 20,
    "get_email": 15,
    "list_actionable": 10,
    "email_stats": 8,
    "risk_overview": 5,
    "find_files": 5,
    "find_knowledge": 2,
}


@dataclass
class Stats:
    times: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    outcomes: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    rows: list[tuple[float, str, str, float]] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)

    def add(self, tool: str, outcome: str, ms: float) -> None:
        self.outcomes[tool][outcome] += 1
        if outcome == "ok":
            self.times[tool].append(ms)
        self.rows.append((time.time(), tool, outcome, ms))


def arguments(tool: str, ids: list[str]) -> dict[str, Any] | None:
    today = datetime.now(UTC).date()
    days = random.choice([1, 3, 7, 30, 90])
    since = (today - timedelta(days=days)).isoformat()
    if tool == "search_email":
        args: dict[str, Any] = {"query": random.choice(QUERIES), "limit": 10}
        if random.random() < 0.5:
            args["since"] = since
        return args
    if tool == "list_email":
        return {"since": since, "limit": 25}
    if tool == "get_email":
        return {"communication_id": random.choice(ids), "max_chars": 4000} if ids else None
    if tool == "list_actionable":
        return {"hours": random.choice([24, 72, 168])}
    if tool == "email_stats":
        return {"group_by": random.choice(["day", "sender_domain", "label", "property"]),
                "since_days": random.choice([7, 30, 90])}  # fmt: skip
    if tool == "risk_overview":
        return {"days": random.choice([7, 30, 90])}
    if tool == "find_files":
        return {"query": random.choice(QUERIES), "since_days": 90}
    if tool == "find_knowledge":
        return {"query": random.choice(["ridge", "capital", "construction", "llc"])}
    raise ValueError(tool)


def outcome_of(response: httpx.Response) -> tuple[str, Any]:
    if response.status_code != 200:
        return f"http_{response.status_code}", None
    try:
        result = response.json().get("result") or {}
    except ValueError:
        return "bad_json", None
    if result.get("isError"):
        return "tool_error", None
    data = result.get("structuredContent")
    if data is None:
        try:
            data = json.loads(result["content"][0]["text"])
        except KeyError, IndexError, ValueError, TypeError:
            return "ok", None  # a file or other non-JSON answer
    if isinstance(data, dict) and data.get("too_slow"):
        return "too_slow", data
    if isinstance(data, dict) and "error" in data:
        return "answered_error", data
    return "ok", data


def collect_ids(data: Any, ids: list[str]) -> None:
    if not isinstance(data, dict):
        return
    for key in ("results", "actions", "risks", "files"):
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("communication_id") and len(ids) < 2000:
                ids.append(str(item["communication_id"]))


async def agent(
    n: int, client: httpx.AsyncClient, url: str, token: str, stats: Stats,
    end: float, ramp: float, think: float, agents: int,
) -> None:  # fmt: skip
    await asyncio.sleep(ramp * n / max(agents, 1))
    headers = HEADERS | {"Authorization": f"Bearer {token}"}
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "fs-ci-loadtest", "version": "1"}}}  # fmt: skip
    with contextlib.suppress(httpx.HTTPError):
        await client.post(url, json=init, headers=headers)
    tools, weights = list(MIX), list(MIX.values())
    call_id = 1
    while time.monotonic() < end:
        tool = random.choices(tools, weights)[0]
        args = arguments(tool, stats.ids)
        if args is None:
            tool, args = "list_email", arguments("list_email", stats.ids)
        call_id += 1
        body = {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                "params": {"name": tool, "arguments": args}}  # fmt: skip
        started = time.monotonic()
        try:
            response = await client.post(url, json=body, headers=headers)
            outcome, data = outcome_of(response)
        except httpx.TimeoutException:
            outcome, data = "client_timeout", None
        except httpx.HTTPError as exc:
            outcome, data = f"network_{type(exc).__name__}", None
        stats.add(tool, outcome, (time.monotonic() - started) * 1000)
        if outcome == "ok":
            collect_ids(data, stats.ids)
        if outcome == "http_401":
            print("Token rejected (401): it expired or is for another API. Stopping.")
            return
        await asyncio.sleep(random.expovariate(1 / think) if think > 0 else 0)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def report(stats: Stats, seconds: float) -> None:
    total = sum(sum(c.values()) for c in stats.outcomes.values())
    print(f"\n{total} calls in {seconds:.0f} s ({total / max(seconds, 1):.1f} per second)\n")
    print(f"{'tool':<16}{'calls':>7}{'ok':>7}{'slow':>6}{'errors':>8}"
          f"{'median':>9}{'p95':>9}{'p99':>9}{'max':>9}   (milliseconds)")  # fmt: skip
    every: list[float] = []
    for tool in sorted(stats.outcomes, key=lambda t: -sum(stats.outcomes[t].values())):
        c, t = stats.outcomes[tool], stats.times[tool]
        every += t
        errors = sum(v for k, v in c.items() if k not in ("ok", "too_slow"))
        print(f"{tool:<16}{sum(c.values()):>7}{c['ok']:>7}{c['too_slow']:>6}{errors:>8}"
              f"{statistics.median(t) if t else 0:>9.0f}{pct(t, 95):>9.0f}"
              f"{pct(t, 99):>9.0f}{max(t) if t else 0:>9.0f}")  # fmt: skip
    print(f"{'all tools':<16}{total:>7}{len(every):>7}{'':>6}{'':>8}"
          f"{statistics.median(every) if every else 0:>9.0f}{pct(every, 95):>9.0f}"
          f"{pct(every, 99):>9.0f}{max(every) if every else 0:>9.0f}")  # fmt: skip
    problems: Counter[str] = Counter()
    for c in stats.outcomes.values():
        problems.update({k: v for k, v in c.items() if k != "ok"})
    if problems:
        print("\nNot ok, by kind:", ", ".join(f"{k} {v}" for k, v in problems.most_common()))
    print(
        "\nRead it as: medians and p95 under a few seconds and no errors means the setup"
        " holds at this load.\n'too_slow' = a tool stopped after 40 s; http_429/503 or"
        " network errors = the app or database ran out of capacity."
    )


def az_token() -> str:
    def az(*args: str) -> str:
        return subprocess.check_output(["az", *args], text=True).strip()

    app_id = az(
        "ad", "app", "list", "--display-name", API_NAME, "--query", "[0].appId", "-o", "tsv"
    )
    if not app_id:
        sys.exit(f"Could not find the app registration '{API_NAME}'. Sign in to the Azure CLI.")
    return az("account", "get-access-token", "--scope", f"api://{app_id}/Communications.Search",
              "--query", "accessToken", "-o", "tsv")  # fmt: skip


async def main_async(a: argparse.Namespace, token: str) -> None:
    stats = Stats()
    limits = httpx.Limits(max_connections=a.agents + 10, max_keepalive_connections=a.agents + 10)
    async with httpx.AsyncClient(timeout=a.timeout, limits=limits) as client:
        started = time.monotonic()
        end = started + a.minutes * 60
        tasks = [
            asyncio.create_task(
                agent(n, client, a.url, token, stats, end, a.ramp, a.think, a.agents)
            )
            for n in range(a.agents)
        ]
        last = 0.0
        while not all(t.done() for t in tasks):
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - last >= 30:
                last = now
                done = len(stats.rows)
                recent = [r for r in stats.rows if r[0] > time.time() - 30]
                ok = [r[3] for r in recent if r[2] == "ok"]
                bad = sum(1 for r in recent if r[2] != "ok")
                rate = len(recent) / 30
                print(f"{now - started:5.0f} s  calls {done:6d}  last 30 s: {rate:5.1f}/s,"
                      f" p95 {pct(ok, 95):6.0f} ms, not ok {bad}")  # fmt: skip
        await asyncio.gather(*tasks)
        seconds = time.monotonic() - started
    report(stats, seconds)
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["unix_time", "tool", "outcome", "ms"])
            w.writerows(stats.rows)
        print(f"Every call (time, tool, outcome, ms) written to {a.csv}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("MCP_URL", DEFAULT_URL))
    p.add_argument("--agents", type=int, default=10, help="simulated agents at once (10)")
    p.add_argument("--minutes", type=float, default=3, help="how long to run (3)")
    p.add_argument(
        "--think",
        type=float,
        default=10,
        help="average seconds an agent waits between calls (10); 0 = flat out",
    )
    p.add_argument("--ramp", type=float, default=30, help="seconds to start all agents (30)")
    p.add_argument("--timeout", type=float, default=90, help="seconds before a call is abandoned")
    p.add_argument("--token", default=os.environ.get("MCP_TOKEN"))
    p.add_argument("--csv", help="also write every call to this CSV file")
    p.add_argument("--yes", action="store_true", help="needed above 20 agents")
    a = p.parse_args()
    remote = not a.url.startswith(("http://127.0.0.1", "http://localhost"))
    if remote and a.agents > 20 and not a.yes:
        sys.exit("More than 20 agents against the live endpoint: add --yes to confirm.")
    token = a.token or az_token()
    print(f"{a.agents} agents for {a.minutes:g} min against {a.url}"
          f" (about one call per agent every {a.think:g} s)")  # fmt: skip
    asyncio.run(main_async(a, token))


if __name__ == "__main__":
    main()
