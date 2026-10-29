"""The OAuth broker in front of Entra: a platform registers with only the MCP URL,
the person confirms and signs in at Microsoft, and the platform gets Entra's own
token. Every refusal path is checked: return addresses outside the approved
platforms, consent without the browser's cookie, wrong PKCE, codes for another
client, expired or tampered values, and accounts without a reader role.

Microsoft is simulated (token endpoint and signing key); the rest is the
production code path, including the MCP SDK's OAuth handlers.
"""

import base64
import hashlib
import secrets
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from ci_core.settings import Settings
from ci_processing.embedding import FakeEmbedder
from ci_retrieval.broker import Sealer
from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import READER_ALL
from ci_retrieval.mcp_server import build_http_app

TENANT = "11111111-1111-1111-1111-111111111111"
API_ID = "22222222-2222-2222-2222-222222222222"
API_URI = f"api://{API_ID}"
BASE = "https://func-fs-ci-search-dev.azurewebsites.net"
BROKER_ID = "33333333-3333-3333-3333-333333333333"
BROKER_SECRET = "broker-test-secret-not-real"  # noqa: S105
CLAUDE = "https://claude.ai/api/mcp/auth_callback"
LOGIN = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticKeys:
    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=KEY.public_key())


def entra_token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": API_ID,
        "tid": TENANT,
        "oid": "will-oid",
        "preferred_username": "will@finishessolutions.example",
        "scp": "Communications.Search",
        "roles": [READER_ALL],
        "azp": BROKER_ID,
        "iat": now,
        "exp": now + 3600,
    } | overrides
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


class FakeEntra:
    """Microsoft's token endpoint: single-use codes, PKCE, the broker's secret."""

    def __init__(self) -> None:
        self.codes: dict[str, str] = {}  # code -> PKCE challenge
        self.claims: dict[str, Any] = {}
        self.requests: list[dict[str, str]] = []

    def issue_code(self, challenge: str) -> str:
        code = secrets.token_urlsafe(16)
        self.codes[code] = challenge
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{LOGIN}/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append(form)
        if form.get("client_id") != BROKER_ID or form.get("client_secret") != BROKER_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        assert form["scope"] == f"{API_URI}/Communications.Search offline_access"
        if form["grant_type"] == "authorization_code":
            challenge = self.codes.pop(form["code"], None)  # single use
            verifier_hash = base64.urlsafe_b64encode(
                hashlib.sha256(form["code_verifier"].encode()).digest()
            ).rstrip(b"=")
            if challenge is None or verifier_hash.decode() != challenge:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "AADSTS70008: x"}
                )
            assert form["redirect_uri"] == f"{BASE}/oauth/callback"
        elif form.get("refresh_token") != "entra-refresh-1":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": entra_token(**self.claims),
                "refresh_token": "entra-refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )


@pytest.fixture
def entra() -> FakeEntra:
    return FakeEntra()


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "deployment_environment": "development",
        "attachment_unsupported_policy": "publish_partial",
        "entra_tenant_id": TENANT,
        "search_public_url": BASE,
        "search_api_client_id": API_ID,
        "search_api_app_id_uri": API_URI,
        "mcp_broker_client_id": BROKER_ID,
        "mcp_broker_client_secret": BROKER_SECRET,
    } | overrides
    return Settings(**values)


@pytest.fixture
def client(entra: FakeEntra) -> Iterator[TestClient]:
    app = build_http_app(
        settings(),
        FakeEmbedder(),
        verifier=EntraTokenVerifier(TENANT, API_ID, API_URI, keys=StaticKeys()),
        connector=lambda: pytest.fail("no database call expected"),
        broker_http=httpx.AsyncClient(transport=httpx.MockTransport(entra.handler)),
    )
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def register(client: TestClient, redirect: str = CLAUDE, **extra: Any) -> Any:
    body = {"redirect_uris": [redirect], "client_name": "Claude"} | extra
    return client.post("/register", json=body)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def sign_in(
    client: TestClient, entra: FakeEntra, reg: dict[str, Any], challenge: str
) -> dict[str, str]:
    """Authorize -> consent page -> Microsoft -> callback; returns the platform's redirect."""
    start = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CLAUDE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "platform-state",
            "resource": f"{BASE}/mcp",
        },
    )
    assert start.status_code == 302
    consent_url = start.headers["location"]
    assert consent_url.startswith(f"{BASE}/oauth/consent?")
    page = client.get(consent_url)
    assert (
        page.status_code == 200 and "Connect Claude to FS Communication Intelligence?" in page.text
    )
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    csrf = page.text.split('name=csrf value="')[1].split('"')[0]
    to_ms = client.post(
        "/oauth/consent", data={"request": query(consent_url)["request"], "csrf": csrf}
    )
    assert to_ms.status_code == 303
    ms = query(to_ms.headers["location"])
    assert to_ms.headers["location"].startswith(f"{LOGIN}/authorize?")
    assert ms["client_id"] == BROKER_ID and ms["redirect_uri"] == f"{BASE}/oauth/callback"
    assert ms["scope"] == f"{API_URI}/Communications.Search offline_access"
    assert ms["code_challenge_method"] == "S256"
    back = client.get(
        "/oauth/callback",
        params={"code": entra.issue_code(ms["code_challenge"]), "state": ms["state"]},
    )
    assert back.status_code == 302 and back.headers["location"].startswith(CLAUDE + "?")
    return query(back.headers["location"])


def redeem(
    client: TestClient, reg: dict[str, Any], code: str, verifier: str, **overrides: str
) -> Any:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE,
        "code_verifier": verifier,
        "client_id": reg["client_id"],
        "client_secret": reg["client_secret"],
    } | overrides
    return client.post("/token", data=form)


def test_metadata_points_platforms_at_the_broker(client: TestClient) -> None:
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["authorization_servers"] == [BASE]
    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == BASE
    assert server["registration_endpoint"] == f"{BASE}/register"
    assert server["code_challenge_methods_supported"] == ["S256"]


def test_without_broker_settings_platforms_go_straight_to_entra() -> None:
    app = build_http_app(
        settings(mcp_broker_client_id=None, mcp_broker_client_secret=None),
        FakeEmbedder(),
        verifier=EntraTokenVerifier(TENANT, API_ID, API_URI, keys=StaticKeys()),
    )
    with TestClient(app, base_url=BASE) as c:
        resource = c.get("/.well-known/oauth-protected-resource/mcp").json()
        assert resource["authorization_servers"] == [
            f"https://login.microsoftonline.com/{TENANT}/v2.0"
        ]
        assert c.post("/register", json={"redirect_uris": [CLAUDE]}).status_code == 404


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/api/mcp/auth_callback",
        "http://claude.ai/api/mcp/auth_callback",  # not https
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "https://claude.ai:8443/api/mcp/auth_callback",
        "https://user@claude.ai/api/mcp/auth_callback",
        "https://claude.ai/other/path",
        "https://chatgpt.com/somewhere-else",
        "https://evil.consent.azure-apim.net.example/redirect",
        "http://localhost:3000/callback",
    ],
)
def test_only_approved_platforms_can_register(client: TestClient, redirect: str) -> None:
    response = register(client, redirect)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "redirect",
    [
        CLAUDE,
        "https://claude.com/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/abc123",
        "https://global.consent.azure-apim.net/redirect",
        "https://global.consent.azure-apim.net/redirect/fs-email",
        "https://unitedstates-002.consent.azure-apim.net/redirect",
    ],
)
def test_approved_platforms_register(client: TestClient, redirect: str) -> None:
    response = register(client, redirect)
    assert response.status_code == 201
    body = response.json()
    assert body["client_id"] and body["client_secret"]
    assert body["redirect_uris"] == [redirect]


def test_full_sign_in_returns_entras_token(client: TestClient, entra: FakeEntra) -> None:
    reg = register(client).json()
    verifier, challenge = pkce()
    back = sign_in(client, entra, reg, challenge)
    assert back["state"] == "platform-state"
    tokens = redeem(client, reg, back["code"], verifier)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["aud"] == API_ID and claims["oid"] == "will-oid"  # Entra's own token
    assert body["refresh_token"] and "entra-refresh-1" not in body["refresh_token"]
    # The code is single-use: Microsoft's code behind it is spent.
    again = redeem(client, reg, back["code"], verifier)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # The MCP endpoint accepts the token (a request without one gets 401).
    assert client.post("/mcp", json={}).status_code == 401
    ok = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {body['access_token']}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert ok.status_code == 200


def test_refresh_goes_through_microsoft_and_rechecks_the_role(
    client: TestClient, entra: FakeEntra
) -> None:
    reg = register(client).json()
    verifier, challenge = pkce()
    first = redeem(client, reg, sign_in(client, entra, reg, challenge)["code"], verifier).json()
    form = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "client_id": reg["client_id"],
        "client_secret": reg["client_secret"],
    }
    assert client.post("/token", data=form).status_code == 200
    assert entra.requests[-1]["refresh_token"] == "entra-refresh-1"  # noqa: S105
    entra.claims = {"roles": []}  # the person's role was removed
    refused = client.post("/token", data=form)
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"
    # Another client can't use this refresh token.
    other = register(client).json()
    stolen = form | {"client_id": other["client_id"], "client_secret": other["client_secret"]}
    assert client.post("/token", data=stolen).status_code == 400


def test_account_without_a_reader_role_gets_no_token(client: TestClient, entra: FakeEntra) -> None:
    entra.claims = {"roles": None}
    reg = register(client).json()
    verifier, challenge = pkce()
    refused = redeem(client, reg, sign_in(client, entra, reg, challenge)["code"], verifier)
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_is_refused(client: TestClient, entra: FakeEntra) -> None:
    reg = register(client).json()
    _, challenge = pkce()
    code = sign_in(client, entra, reg, challenge)["code"]
    refused = redeem(client, reg, code, secrets.token_urlsafe(48))
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_grant"
    assert entra.requests == []  # Microsoft was never asked


def test_code_issued_to_one_client_is_useless_to_another(
    client: TestClient, entra: FakeEntra
) -> None:
    reg = register(client).json()
    other = register(client).json()
    verifier, challenge = pkce()
    code = sign_in(client, entra, reg, challenge)["code"]
    refused = redeem(
        client,
        other,
        code,
        verifier,
        client_id=other["client_id"],
        client_secret=other["client_secret"],
    )
    assert refused.status_code == 400
    assert entra.requests == []


def test_wrong_client_secret_is_refused(client: TestClient, entra: FakeEntra) -> None:
    reg = register(client).json()
    verifier, challenge = pkce()
    code = sign_in(client, entra, reg, challenge)["code"]
    assert redeem(client, reg, code, verifier, client_secret="guess").status_code == 401  # noqa: S106


def test_tampered_client_id_is_unknown(client: TestClient) -> None:
    reg = register(client).json()
    tampered = reg["client_id"][:-4] + ("AAAA" if not reg["client_id"].endswith("AAAA") else "BBBB")
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": tampered,
            "redirect_uri": CLAUDE,
            "code_challenge": pkce()[1],
        },
    )
    assert response.status_code == 400


def test_authorize_requires_pkce_and_a_registered_return_address(client: TestClient) -> None:
    reg = register(client).json()
    no_pkce = client.get(
        "/authorize",
        params={"response_type": "code", "client_id": reg["client_id"], "redirect_uri": CLAUDE},
    )
    assert no_pkce.status_code == 302 and "error=invalid_request" in no_pkce.headers["location"]
    elsewhere = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect",
            "code_challenge": pkce()[1],
        },
    )
    assert elsewhere.status_code == 400  # never redirected to an unregistered address


def test_consent_needs_this_browsers_cookie(client: TestClient) -> None:
    reg = register(client).json()
    start = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CLAUDE,
            "code_challenge": pkce()[1],
        },
    )
    sealed = query(start.headers["location"])["request"]
    # A cross-site form post: no consent page was shown, so no cookie.
    forged = client.post("/oauth/consent", data={"request": sealed, "csrf": "anything"})
    assert forged.status_code == 400
    client.get(start.headers["location"])  # sets this browser's cookie
    wrong = client.post("/oauth/consent", data={"request": sealed, "csrf": "not-the-cookie"})
    assert wrong.status_code == 400


def test_cancel_and_microsoft_errors_return_access_denied(
    client: TestClient, entra: FakeEntra
) -> None:
    reg = register(client).json()
    start = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": CLAUDE,
            "code_challenge": pkce()[1],
            "state": "s1",
        },
    )
    page = client.get(start.headers["location"])
    assert f'href="{CLAUDE}?error=access_denied&amp;state=s1"' in page.text
    csrf = page.text.split('name=csrf value="')[1].split('"')[0]
    to_ms = client.post(
        "/oauth/consent",
        data={"request": query(start.headers["location"])["request"], "csrf": csrf},
    )
    state = query(to_ms.headers["location"])["state"]
    back = client.get(
        "/oauth/callback",
        params={
            "error": "access_denied",
            "error_description": "AADSTS50105: The signed in user is not assigned",
            "state": state,
        },
    )
    assert back.status_code == 302
    assert query(back.headers["location"])["error"] == "access_denied"
    assert query(back.headers["location"])["state"] == "s1"


def test_expired_or_forged_values_are_refused(client: TestClient) -> None:
    assert client.get("/oauth/callback", params={"code": "x", "state": "forged"}).status_code == 400
    assert client.get("/oauth/consent", params={"request": "forged"}).status_code == 400
    sealer = Sealer(BROKER_SECRET)
    expired = sealer.seal("state", {"r": CLAUDE}, ttl=-1)
    assert client.get("/oauth/callback", params={"code": "x", "state": expired}).status_code == 400
    # A value sealed for one purpose can't be used for another.
    as_code = sealer.seal("code", {"r": CLAUDE}, ttl=60)
    assert sealer.open("state", as_code) is None
    # A different broker secret (rotated) opens nothing.
    assert Sealer("another-secret").open("code", as_code) is None
