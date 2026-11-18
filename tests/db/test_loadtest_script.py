"""The MCP load test (scripts/loadtest/mcp_load.py) against a real local server: a
few simulated agents for a few seconds on the synthetic fixtures. Checks the
script still speaks the endpoint's protocol and counts what it sees."""

import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from test_hosted_mcp import API_ID, API_URI, TENANT, VERIFIER, database_ready, token  # noqa: F401

from ci_core.db import connect
from ci_core.settings import Settings
from ci_processing.embedding import FakeEmbedder
from ci_retrieval.mcp_server import build_http_app

SCRIPT = Path(__file__).parents[2] / "scripts" / "loadtest" / "mcp_load.py"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("mcp_load", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_load"] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


def test_load_script_against_a_local_server(database_ready: str) -> None:  # noqa: F811
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        deployment_environment="development",
        attachment_unsupported_policy="publish_partial",
        entra_tenant_id=TENANT,
        search_public_url=base,
        search_api_client_id=API_ID,
        search_api_app_id_uri=API_URI,
        admin_public_url="https://admin.example",
    )
    app = build_http_app(
        settings,
        FakeEmbedder(),
        verifier=VERIFIER,
        connector=lambda: connect(dbname=database_ready, role="ci_search"),
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        mcp_load = load_script()
        stats = mcp_load.Stats()

        async def run() -> None:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                end = time.monotonic() + 4
                tasks = [
                    mcp_load.agent(n, client, f"{base}/mcp", token(), stats, end, 0.5, 0.05, 4)
                    for n in range(4)
                ]
                import asyncio

                await asyncio.gather(*tasks)

        import asyncio

        asyncio.run(run())
        calls = sum(sum(c.values()) for c in stats.outcomes.values())
        bad = {t: dict(c) for t, c in stats.outcomes.items() if set(c) - {"ok"}}
        assert calls >= 8 and not bad, bad
        assert stats.ids  # results fed get_email calls
        mcp_load.report(stats, 4)  # the summary prints without failing
    finally:
        server.should_exit = True
        thread.join(timeout=10)
