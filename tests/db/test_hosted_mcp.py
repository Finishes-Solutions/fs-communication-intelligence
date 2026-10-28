"""The hosted MCP endpoint: Entra token checks, OAuth discovery, tools over HTTP,
and the same app served through the Azure Functions ASGI adapter.

Tokens are signed here with a throwaway RSA key standing in for Entra's
signing keys; everything else is the production code path.
"""

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import azure.functions as func
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener
from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import READER_ALL
from ci_retrieval.mcp_server import build_http_app

TENANT = "11111111-1111-1111-1111-111111111111"
API_ID = "22222222-2222-2222-2222-222222222222"
API_URI = f"api://{API_ID}"
BASE = "https://func-fs-ci-search-dev.azurewebsites.net"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}


class StaticKeys:
    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=KEY.public_key())


def token(signing_key: Any = KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": API_ID,
        "tid": TENANT,
        "oid": "chuck-oid",
        "preferred_username": "Chuck@FinishesSolutions.example",
        "scp": "Communications.Search",
        "roles": [READER_ALL],
        "azp": "chatgpt-client",
        "iat": now,
        "exp": now + 3600,
    } | overrides
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, signing_key, algorithm="RS256")


VERIFIER = EntraTokenVerifier(TENANT, API_ID, API_URI, keys=StaticKeys())


@pytest.mark.parametrize(
    ("claims", "key", "rejected_for"),
    [
        ({}, KEY, None),
        ({"aud": API_URI}, KEY, None),
        ({"aud": "some-other-api"}, KEY, "audience"),
        ({"iss": "https://login.microsoftonline.com/evil/v2.0"}, KEY, "issuer"),
        ({"tid": "33333333-3333-3333-3333-333333333333"}, KEY, "tenant"),
        ({"scp": None}, KEY, "scope"),  # app-only token: no delegated user
        ({"scp": "User.Read"}, KEY, "scope"),
        ({"exp": int(time.time()) - 600}, KEY, "expired"),
        ({}, OTHER_KEY, "signature"),  # not signed by the tenant's key
    ],
)
def test_token_rules(
    claims: dict[str, Any],
    key: Any,
    rejected_for: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = token(key, **claims)
    with caplog.at_level("WARNING", logger="ci_retrieval.entra"):
        result = VERIFIER.verify(raw)
    assert (result is None) is (rejected_for is not None)
    if result:
        assert result.principal().oid == "chuck-oid"
        assert result.principal().upn == "chuck@finishessolutions.example"
        assert result.scopes == [f"{API_URI}/Communications.Search"]
        assert caplog.records == []
        return
    (record,) = caplog.records
    message = record.getMessage()
    assert message.startswith(f"token rejected: {rejected_for};")
    # The log says what the token was for, never the token or who it belongs to.
    assert raw not in message and raw.split(".")[2] not in message
    assert "chuck" not in message.lower()
    assert "azp" in message or rejected_for == "signature"


def test_unreadable_tokens_are_logged_without_content(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="ci_retrieval.entra"):
        assert VERIFIER.verify("not-a-jwt") is None
    (record,) = caplog.records
    assert record.getMessage().startswith("token rejected: malformed;")
    assert "not-a-jwt" not in record.getMessage()


@pytest.fixture(scope="module")
def database_ready(database: str) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(
        connector,
        graph,
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    process_all(
        lambda: connector("ci_ingest"),
        Backends(
            FakeScreener(), FakeClassifier(), FakeEmbedder(), graph=graph, extraction_enabled=True
        ),
        workers=1,
    )
    yield database


@pytest.fixture
def app(database_ready: str) -> Any:
    """A fresh app per test: an MCP HTTP app can start (lifespan) only once."""
    settings = Settings(
        deployment_environment="development",
        attachment_unsupported_policy="publish_partial",
        entra_tenant_id=TENANT,
        search_public_url=BASE,
        search_api_client_id=API_ID,
        search_api_app_id_uri=API_URI,
        admin_public_url="https://admin.example",
    )
    return build_http_app(
        settings,
        FakeEmbedder(),
        verifier=VERIFIER,
        connector=lambda: connect(dbname=database_ready, role="ci_search"),
    )


def rpc(method: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


INIT = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1"},
    },
)


def test_discovery_and_unauthenticated_requests(app: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="ci_retrieval.mcp_server")
    with TestClient(app, base_url=BASE) as client:
        response = client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert "resource_metadata=" in challenge
        metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
        metadata = client.get(metadata_url.replace(BASE, "")).json()
        assert metadata["resource"].rstrip("/") == f"{BASE}/mcp"
        assert (
            metadata["authorization_servers"][0].rstrip("/")
            == f"https://login.microsoftonline.com/{TENANT}/v2.0"
        )
        assert f"{API_URI}/Communications.Search" in metadata["scopes_supported"]
        bad = client.post(
            "/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": f"Bearer {token(OTHER_KEY)}"}
        )
        assert bad.status_code == 401
    requests = [r.getMessage() for r in caplog.records if r.name == "ci_retrieval.mcp_server"]
    assert "mcp request POST /mcp -> 401 (bearer token: no)" in requests
    assert "mcp request POST /mcp -> 401 (bearer token: yes)" in requests
    assert any(
        m.startswith("mcp request GET /.well-known/oauth-protected-resource") for m in requests
    )
    assert not any("Bearer" in m or "eyJ" in m for m in requests)


def call_tool(client: TestClient, bearer: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    headers = MCP_HEADERS | {"Authorization": f"Bearer {bearer}"}
    assert client.post("/mcp", json=INIT, headers=headers).status_code == 200
    response = client.post(
        "/mcp", json=rpc("tools/call", {"name": name, "arguments": args}, 2), headers=headers
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    data: dict[str, Any] = result.get("structuredContent") or json.loads(
        result["content"][0]["text"]
    )
    return data


def test_tools_use_the_callers_identity(app: Any) -> None:
    with TestClient(app, base_url=BASE) as client:
        headers = MCP_HEADERS | {"Authorization": f"Bearer {token()}"}
        client.post("/mcp", json=INIT, headers=headers)
        listed = client.post("/mcp", json=rpc("tools/list", {}, 3), headers=headers).json()
        assert {t["name"] for t in listed["result"]["tools"]} == {
            "search_email",
            "get_email",
            "get_attachment",
            "list_email",
            "list_actionable",
        }
        report = call_tool(
            client,
            token(),
            "list_actionable",
            {"person": "pat.example@finishessolutions.example", "hours": 24 * 60},
        )
        assert "Invoice 4471 for Lot 12 grading - payment due Friday" in {
            a["subject"] for a in report["actions"]
        }
        invoice = next(a for a in report["actions"] if a["subject"].startswith("Invoice 4471"))
        assert invoice["view_url"] == f"https://admin.example/read/{invoice['communication_id']}"
        email = call_tool(
            client, token(), "get_email", {"communication_id": invoice["communication_id"]}
        )
        (pdf,) = [a for a in email["attachments"] if a["name"] == "Invoice-4471.pdf"]
        text = call_tool(
            client,
            token(),
            "get_attachment",
            {
                "communication_id": invoice["communication_id"],
                "attachment_id": pdf["attachment_id"],
            },
        )
        assert text["total_chars"] > 0 and text["view_url"] == invoice["view_url"]
        # The same request from someone with no role and no grant finds nothing.
        empty = call_tool(
            client, token(roles=[], oid="stranger-oid"), "list_actionable", {"hours": 24 * 60}
        )
        assert empty["actions"] == [] and empty["risks"] == []


def test_served_through_the_azure_functions_adapter(app: Any) -> None:
    middleware = func.AsgiMiddleware(app)  # type: ignore[no-untyped-call]

    async def send(body: dict[str, Any], bearer: str | None) -> func.HttpResponse:
        headers = dict(MCP_HEADERS) | ({"Authorization": f"Bearer {bearer}"} if bearer else {})
        request = func.HttpRequest(
            "POST", f"{BASE}/mcp", headers=headers, body=json.dumps(body).encode()
        )
        response: func.HttpResponse = await middleware.handle_async(request)
        return response

    async def scenario() -> tuple[int, int, dict[str, Any]]:
        assert await middleware.notify_startup()  # type: ignore[no-untyped-call]
        anonymous = await send(INIT, None)
        signed = await send(INIT, token())
        tools = await send(rpc("tools/list", {}, 2), token())
        return anonymous.status_code, signed.status_code, json.loads(tools.get_body())

    anonymous, signed, tools = asyncio.run(scenario())
    assert (anonymous, signed) == (401, 200)
    assert len(tools["result"]["tools"]) == 5
