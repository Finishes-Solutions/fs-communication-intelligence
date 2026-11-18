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
from zoneinfo import ZoneInfo

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
from ci_processing.originals import DirectoryStore
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
def database_ready(database: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
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
            FakeScreener(),
            FakeClassifier(),
            FakeEmbedder(),
            graph=graph,
            extraction_enabled=True,
            originals=DirectoryStore(tmp_path_factory.mktemp("originals")),
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
            "get_thread",
            "list_email",
            "list_actionable",
            "email_stats",
            "risk_overview",
            "find_knowledge",
            "get_profile",
            "get_file_links",
            "find_files",
            "export_email",
            "get_download_links",
            "get_file",
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
    assert len(tools["result"]["tools"]) == 15


def test_threads_stats_risks_and_date_filters(app: Any, database_ready: str) -> None:
    with TestClient(app, base_url=BASE) as client:
        found = call_tool(client, token(), "search_email", {"query": "change order"})
        co7 = next(r for r in found["results"] if r["subject"].startswith("Change Order 7"))
        co8 = next(r for r in found["results"] if r["subject"].startswith("Change Order 8"))
        alone = call_tool(
            client, token(), "get_thread", {"communication_id": co8["communication_id"]}
        )
        assert alone["count"] == 1  # three mailbox copies of one message, collapsed
        with connect(dbname=database_ready) as conn:  # make CO7 a reply in CO8's thread
            conn.execute(
                "UPDATE email_messages SET conversation_id = 'AAQkAGsynconvco8'"
                " WHERE communication_id = %s",
                (co7["communication_id"],),
            )
            conn.commit()
        try:
            thread = call_tool(
                client, token(), "get_thread", {"communication_id": co8["communication_id"]}
            )
            stranger = call_tool(
                client,
                token(roles=[], oid="stranger-oid"),
                "get_thread",
                {"communication_id": co8["communication_id"]},
            )
        finally:
            with connect(dbname=database_ready) as conn:
                conn.execute(
                    "UPDATE email_messages SET conversation_id = 'AAQkAGsynconv0002'"
                    " WHERE communication_id = %s",
                    (co7["communication_id"],),
                )
                conn.commit()
        times = [m["received_at"] for m in thread["messages"]]
        assert thread["count"] == 2 and times == sorted(times)  # oldest first
        assert all(
            m["view_url"].startswith("https://admin.example/read/") for m in thread["messages"]
        )
        assert stranger == {"found": False, "note": "Not found in authorized, indexed mail."}

        stats = call_tool(
            client, token(), "email_stats", {"group_by": "property", "since_days": 365, "limit": 3}
        )
        assert stats["group_by"] == "property" and 0 < len(stats["groups"]) <= 3
        assert stats["total_groups"] >= len(stats["groups"])
        assert {"key", "messages", "actions", "risks"} <= set(stats["groups"][0])
        risks = call_tool(client, token(), "risk_overview", {"days": 365, "item_limit": 2})
        assert risks["total"] >= 1 and len(risks["items"]) <= 2
        assert risks["items"][0]["view_url"].startswith("https://admin.example/read/")
        assert len(risks["matrix"]["cells"]) == 9  # unrated here: no rating step ran
        assert risks["matrix"]["unrated"] == risks["total"]
        unrated = call_tool(client, token(), "risk_overview", {"days": 365, "impact": "unrated"})
        assert unrated["total"] == risks["total"]

        found = call_tool(client, token(), "find_knowledge", {"query": "northfield"})
        org = next(r for r in found["results"] if r["kind"] == "organization")
        assert org["name"] == "Northfield Framing Co." and "without human review" in found["note"]
        profile = call_tool(client, token(), "get_profile", {"entity_id": org["entity_id"]})
        assert profile["entity"]["entity_id"] == org["entity_id"]
        assert any(f["relation"] == "works_for" for f in profile["facts"])
        assert all(
            e["view_url"].startswith("https://admin.example/read/")
            for e in profile["recent_emails"]
        )
        assert profile["facts"][0]["view_url"].startswith("https://admin.example/read/")
        invoice = next(
            r
            for r in call_tool(client, token(), "search_email", {"query": "grading invoice"})[
                "results"
            ]
            if r["subject"].startswith("Invoice 4471")
        )
        links = call_tool(
            client,
            token(),
            "get_file_links",
            {
                "communication_ids": [
                    invoice["communication_id"],
                    "00000000-0000-0000-0000-000000000000",  # nobody's email
                    "not-a-uuid",
                ]
            },
        )
        assert links["not_found"] == 2 and len(links["emails"]) == 1
        email = links["emails"][0]
        assert email["view_url"] == f"https://admin.example/read/{invoice['communication_id']}"
        pdf = next(f for f in email["files"] if f["name"].endswith(".pdf"))
        assert pdf["download_url"].startswith(email["view_url"] + "?file=")
        assert "text" not in email and "admins" in links["note"]
        missing = call_tool(client, token(), "get_profile", {"entity_id": 999_999})
        assert missing == {"found": False, "note": "Not found in authorized, indexed mail."}

        # Exact dates: the fixture mail is from September and October 2026.
        october = call_tool(
            client,
            token(),
            "list_email",
            {"since_date": "2026-10-01", "until_date": "2026-10-31", "limit": 100},
        )
        before = call_tool(
            client,
            token(),
            "list_email",
            {"since_date": "2000-01-01", "until_date": "2000-01-31", "limit": 100},
        )
        assert october["results"] and before["results"] == []
        assert all(r["received_at"] >= "2026-10-01" for r in october["results"])


def raw_tool(client: TestClient, bearer: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The tool's whole MCP result (content blocks), for answers that carry a file."""
    headers = MCP_HEADERS | {"Authorization": f"Bearer {bearer}"}
    assert client.post("/mcp", json=INIT, headers=headers).status_code == 200
    response = client.post(
        "/mcp", json=rpc("tools/call", {"name": name, "arguments": args}, 2), headers=headers
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def agent_app(database: str, max_bytes: int, fetched: list[str]) -> Any:
    """The MCP app with get_file reading files through a stand-in for the console."""

    def fetch(url: str) -> bytes | None:
        fetched.append(url)
        return b"%PDF-1.4 stand-in for the stored file"

    settings = Settings(
        deployment_environment="development",
        attachment_unsupported_policy="publish_partial",
        entra_tenant_id=TENANT,
        search_public_url=BASE,
        search_api_client_id=API_ID,
        search_api_app_id_uri=API_URI,
        admin_public_url="https://admin.example",
        mcp_inline_file_max_bytes=max_bytes,
    )
    return build_http_app(
        settings,
        FakeEmbedder(),
        verifier=VERIFIER,
        connector=lambda: connect(dbname=database, role="ci_search"),
        fetch_file=fetch,
    )


def test_dates_times_ranges_and_paging_by_time(app: Any) -> None:
    """since/until take dates or dates and times (Central unless offset), on every tool
    that lists or counts; list_email pages a long period with next_until."""
    with TestClient(app, base_url=BASE) as client:

        def tool(name: str, **args: Any) -> dict[str, Any]:
            return call_tool(client, token(), name, args)

        everything = tool("list_email", since="2026-09-01", until="2026-10-31", limit=100)
        stamps = sorted(r["received_at"] for r in everything["results"])
        assert len(stamps) >= 6
        # A window ending at a time keeps only what came before that moment.
        middle = datetime.fromisoformat(stamps[len(stamps) // 2])
        earlier = tool("list_email", since="2026-09-01", until=middle.isoformat(), limit=100)
        assert earlier["results"]
        assert all(datetime.fromisoformat(r["received_at"]) < middle for r in earlier["results"])
        later = tool("list_email", since=middle.isoformat(), until="2026-10-31", limit=100)
        assert all(datetime.fromisoformat(r["received_at"]) >= middle for r in later["results"])
        assert len(earlier["results"]) + len(later["results"]) == len(stamps)

        # One day by date: from midnight to midnight Central.
        first = datetime.fromisoformat(stamps[0]).astimezone(ZoneInfo("America/Chicago"))
        day = first.date().isoformat()
        one_day = tool("list_email", since=day, until=day, limit=100)
        local = {
            datetime.fromisoformat(r["received_at"]).astimezone(ZoneInfo("America/Chicago")).date()
            for r in one_day["results"]
        }
        assert one_day["results"] and local == {first.date()}

        # Paging a long period by time reaches every email, two at a time.
        seen: set[str] = set()
        args: dict[str, Any] = {"since": "2026-09-01", "until": "2026-10-31", "limit": 2}
        for _ in range(100):
            page = tool("list_email", **args)
            seen |= {r["communication_id"] for r in page["results"]}
            if not page["has_more"]:
                break
            args["until"] = page["next_until"]
        assert seen == {r["communication_id"] for r in everything["results"]}

        # The same window on the other tools.
        actions = tool("list_actionable", since="2026-09-01", until="2026-10-31", status="all")
        none = tool("list_actionable", since="2000-01-01", until="2000-01-31", status="all")
        assert actions["actions"] or actions["risks"]
        assert not (none["actions"] or none["risks"] or none["possible"])
        risks = tool("risk_overview", since="2026-09-01", until="2026-10-31")
        assert risks["since"].startswith("2026-09-01T05:00") and risks["until"].startswith(
            "2026-11-01T05:00"
        )
        stats = tool("email_stats", group_by="month", since="2026-09-01", until="2026-10-31")
        assert sum(g["messages"] for g in stats["groups"]) >= 1
        found = tool("search_email", query="invoice", since="2026-09-01T00:00Z")
        assert found["results"]
        bad = raw_tool(client, token(), "list_email", {"since": "last tuesday"})
        assert "since must be a date like 2026-10-01" in json.dumps(bad)


def test_polling_by_processing_time_with_a_cursor(app: Any, database_ready: str) -> None:
    """list_email(processed_since=..., cursor=...) for pollers such as the AMP relay:
    oldest processed first, every email once, and a reprocessed email comes again."""
    with TestClient(app, base_url=BASE) as client:

        def tool(name: str, **args: Any) -> dict[str, Any]:
            return call_tool(client, token(), name, args)

        everything = tool("list_email", since="2000-01-01", limit=100)
        assert all(r["processed_at"] >= r["received_at"] for r in everything["results"])
        subjects = {r["subject"] for r in everything["results"]}

        seen: list[dict[str, Any]] = []
        args: dict[str, Any] = {"processed_since": "2000-01-01", "limit": 2}
        for _ in range(100):
            page = tool("list_email", **args)
            seen += page["results"]
            args = {"cursor": page["next_cursor"], "limit": 2}
            if not page["has_more"]:
                break
        stamps = [r["processed_at"] for r in seen]
        assert stamps == sorted(stamps)  # oldest processed first
        assert {r["subject"] for r in seen} == subjects  # every email (copies may repeat)
        assert len({r["communication_id"] for r in seen}) == len(seen)  # none twice

        # Nothing new since the last cursor; then one email is processed again.
        quiet = tool("list_email", cursor=args["cursor"])
        assert quiet["results"] == [] and quiet["has_more"] is False
        again = everything["results"][-1]["communication_id"]
        with connect(dbname=database_ready) as conn:
            conn.execute(
                "UPDATE communication_versions v SET published_at = now() + interval '1 minute'"
                " FROM communications c WHERE c.active_version_id = v.id AND c.id = %s",
                (again,),
            )
            conn.commit()
        fresh = tool("list_email", cursor=quiet["next_cursor"])
        assert [r["communication_id"] for r in fresh["results"]] == [again]

        # Polling keeps the other filters, and a forged cursor is refused clearly.
        none = tool("list_email", processed_since="2000-01-01", mailbox="nobody@example.com")
        assert none["results"] == []
        bad = raw_tool(client, token(), "list_email", {"cursor": "not-a-cursor"})
        assert "cursor is not one this server gave out" in json.dumps(bad)
        later = tool("list_email", processed_since="2100-01-01")
        assert later["results"] == [] and later["next_cursor"]


def test_agents_find_files_and_get_emails_as_documents(app: Any) -> None:
    import base64
    import io
    import zipfile

    with TestClient(app, base_url=BASE) as client:
        found = call_tool(client, token(), "find_files", {"query": "invoice", "since_days": None})
        (invoice,) = [f for f in found["files"] if f["name"] == "Invoice-4471.pdf"]
        assert invoice["file_id"] and invoice["view_url"].startswith("https://admin.example/read/")
        assert invoice["download_url"].endswith(f"?file={invoice['file_id']}")
        text = call_tool(
            client,
            token(),
            "find_files",
            {"query": "plumbing", "search_in": "text", "since_days": None},
        )
        assert [f["name"] for f in text["files"]] == ["CO-008-BuildingD.pdf"]
        by_company = call_tool(
            client, token(), "find_files", {"company": "acme-sitework", "since_days": None}
        )
        assert by_company["files"] and all(
            f["sender"].endswith("acme-sitework.example") for f in by_company["files"]
        )
        assert call_tool(client, token(roles=[]), "find_files", {"since_days": None})["files"] == []

        cid = invoice["communication_id"]
        md = call_tool(client, token(), "export_email", {"communication_id": cid})
        assert md["markdown"].startswith("# Invoice 4471") and md["file_name"].endswith(".md")
        assert "## Attachments" in md["markdown"] and md["next_offset"] is None
        page = call_tool(
            client, token(), "export_email", {"communication_id": cid, "max_chars": 1000}
        )
        assert len(page["markdown"]) <= 1000 and page["total_chars"] == md["total_chars"]

        word = raw_tool(
            client, token(), "export_email", {"communication_id": cid, "format": "docx"}
        )
        info = json.loads(word["content"][0]["text"])
        assert info["file_name"].endswith(".docx") and "/dl/" in info["fetch_url"]
        blob = word["content"][1]["resource"]
        assert (
            blob["mimeType"]
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        package = zipfile.ZipFile(io.BytesIO(base64.b64decode(blob["blob"])))
        assert "Invoice 4471" in package.read("word/document.xml").decode()

        missing = call_tool(client, token(roles=[]), "export_email", {"communication_id": cid})
        assert missing["found"] is False


def test_download_links_and_files_inside_the_answer(database_ready: str) -> None:
    import base64

    fetched: list[str] = []
    with TestClient(agent_app(database_ready, 4_000_000, fetched), base_url=BASE) as client:
        found = call_tool(client, token(), "find_files", {"query": "invoice", "since_days": None})
        invoice = next(f for f in found["files"] if f["name"] == "Invoice-4471.pdf")
        cid, file_id = invoice["communication_id"], invoice["file_id"]

        links = call_tool(
            client,
            token(),
            "get_download_links",
            {"communication_ids": [cid], "email_format": "docx"},
        )
        (email,) = links["emails"]
        assert email["email_file"]["fetch_url"].startswith("https://admin.example/dl/")
        pdf = next(f for f in email["files"] if f.get("file_id") == file_id)
        assert pdf["fetch_url"].startswith("https://admin.example/dl/") and pdf["fetch_expires_at"]
        bad = call_tool(
            client,
            token(),
            "get_download_links",
            {"communication_ids": [cid], "email_format": "exe"},
        )
        assert "error" in bad
        stranger = call_tool(
            client,
            token(roles=[], oid="stranger-oid"),
            "get_download_links",
            {"communication_ids": [cid]},
        )
        assert stranger.get("emails", []) == []

        result = raw_tool(
            client, token(), "get_file", {"communication_id": cid, "file_id": file_id}
        )
        info = json.loads(result["content"][0]["text"])
        assert info["inline"] is True and info["file_name"] == "Invoice-4471.pdf"
        assert fetched and fetched[-1].startswith("https://admin.example/dl/")
        resource = result["content"][1]["resource"]
        assert resource["mimeType"] == "application/pdf"
        assert base64.b64decode(resource["blob"]).startswith(b"%PDF")
        nothing = call_tool(
            client, token(roles=[]), "get_file", {"communication_id": cid, "file_id": file_id}
        )
        assert nothing["found"] is False

    fetched.clear()
    with TestClient(agent_app(database_ready, 10, fetched), base_url=BASE) as client:
        big = call_tool(client, token(), "get_file", {"communication_id": cid, "file_id": file_id})
        assert big["inline"] is False and "fetch_url" in big and fetched == []
