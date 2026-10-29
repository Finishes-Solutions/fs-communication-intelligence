"""OAuth broker: Claude, ChatGPT and Copilot Studio connect with only the MCP URL.

The platforms set themselves up with dynamic client registration (RFC 7591), which
Microsoft Entra does not offer. This module is a thin authorization server in
front of Entra (CLAUDE.md §8, ADR-021), built on the MCP SDK's OAuth handlers,
which check PKCE (S256), redirect URIs, code expiry and client secrets:

- ``/register``: a platform registers itself. Only return addresses of the
  approved platforms (``ALLOWED_REDIRECTS``) are accepted. Nothing is stored:
  the client id is a sealed record of the registration, its secret derived from it.
- ``/authorize`` -> ``/oauth/consent``: the person sees which platform is
  connecting and continues to Microsoft, through one confidential Entra client,
  the broker's.
- ``/oauth/callback``: Entra's code is sealed, with the platform's PKCE
  challenge, into the code the platform receives.
- ``/token``: the SDK checks the platform's secret and PKCE, then Entra's code is
  redeemed. Entra codes are single-use, so ours are too.

Who gets in is still decided by Entra and the token check, not here: Entra
refuses anyone not assigned to the search API ("assignment required"), and the
access tokens handed out are Entra's own, validated on every MCP call exactly as
before (``ci_retrieval.entra``). On top of that the broker refuses a sign-in whose
token has no reader role. It mints no tokens, keeps no state (the search
identity has no database write access, L4) and never logs a code or token.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from ci_retrieval.entra import EntraTokenVerifier
from ci_retrieval.identity import READER_ALL, READER_OWN

log = logging.getLogger(__name__)

CALLBACK_PATH = "/oauth/callback"
CONSENT_PATH = "/oauth/consent"
_COOKIE = "__Host-fsci-consent"
_MAX_SEALED = 8192  # longest sealed value accepted (an Entra refresh token is about 2 KB)

# Return addresses the broker sends sign-ins back to: host, then path (a path ending
# in "/" is a prefix). Every host is the platform's own, so a sign-in can only ever
# land with Claude, ChatGPT or Copilot Studio. MCP_BROKER_EXTRA_REDIRECTS adds more.
ALLOWED_REDIRECTS: tuple[tuple[str, str, str], ...] = (
    ("Claude", "claude.ai", "/api/mcp/auth_callback"),
    ("Claude", "claude.com", "/api/mcp/auth_callback"),
    ("ChatGPT", "chatgpt.com", "/connector_platform_oauth_redirect"),
    ("ChatGPT", "chatgpt.com", "/connector/oauth/"),
    ("Copilot Studio", "global.consent.azure-apim.net", "/redirect"),
    ("Copilot Studio", "global.consent.azure-apim.net", "/redirect/"),
)
# Power Platform's regional consent hosts (<region>.consent.azure-apim.net) are
# Microsoft's own and serve Copilot Studio's connector sign-ins outside "global".
_COPILOT_REGIONAL = re.compile(r"^[a-z0-9-]+\.consent\.azure-apim\.net$")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sealer:
    """Authenticated encryption (AES-256-GCM) for the values the broker hands out
    instead of storing them. The purpose is bound in, so a code can't pass as a
    client id or a refresh token. The key is derived from the broker's Entra client
    secret: rotating that secret ends every connection (people sign in again)."""

    def __init__(self, secret: str) -> None:
        key = HKDF(
            algorithm=hashes.SHA256(), length=64, salt=None, info=b"fs-ci mcp broker v1"
        ).derive(secret.encode())
        self._aead = AESGCM(key[:32])
        self._mac_key = key[32:]

    def seal(self, purpose: str, data: dict[str, Any], ttl: int | None = None) -> str:
        body = dict(data)
        if ttl is not None:
            body["exp"] = int(time.time()) + ttl
        nonce = os.urandom(12)
        plain = json.dumps(body, separators=(",", ":")).encode()
        return _b64(nonce + self._aead.encrypt(nonce, plain, purpose.encode()))

    def open(self, purpose: str, value: str | None) -> dict[str, Any] | None:
        if not value or len(value) > _MAX_SEALED:
            return None
        try:
            raw = _unb64(value)
            data = json.loads(self._aead.decrypt(raw[:12], raw[12:], purpose.encode()))
        except InvalidTag, ValueError, binascii.Error:
            return None
        if not isinstance(data, dict):
            return None
        if "exp" in data and int(data["exp"]) < time.time():
            return None
        return data

    def mac(self, value: str) -> str:
        return _b64(hmac.new(self._mac_key, value.encode(), hashlib.sha256).digest())


class BrokerCode(AuthorizationCode):
    entra_code: str
    entra_verifier: str


class BrokerRefreshToken(RefreshToken):
    entra_refresh_token: str


def _aadsts(text: str) -> str:
    """The AADSTS error number from an Entra error description (safe to log)."""
    found = re.search(r"AADSTS\d+", text)
    return found.group(0) if found else "no code"


class EntraBroker:
    """``OAuthAuthorizationServerProvider`` for the MCP SDK, fronting Entra."""

    def __init__(
        self,
        *,
        base_url: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        verifier: EntraTokenVerifier,
        extra_redirects: tuple[str, ...] = (),
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.callback = f"{self.base}{CALLBACK_PATH}"
        self.login = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0"
        self.client_id = client_id
        self._client_secret = client_secret
        self.verifier = verifier
        # Entra issues tokens for the search API only; what a platform asks for is ignored.
        self.entra_scope = f"{verifier.scope_uri} offline_access"
        self._seal = Sealer(client_secret)
        self._extra = tuple(u for u in extra_redirects if u.startswith("https://"))
        self._http = http

    # ----- return addresses ----------------------------------------------------

    def platform(self, uri: str) -> str | None:
        """The platform a return address belongs to, or None if it isn't allowed."""
        try:
            parts = urlsplit(uri)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme != "https" or port is not None or parts.fragment:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        host = (parts.hostname or "").lower()
        for name, allowed_host, path in ALLOWED_REDIRECTS:
            if host == allowed_host and (
                parts.path == path or (path.endswith("/") and parts.path.startswith(path))
            ):
                return name
        if _COPILOT_REGIONAL.match(host) and (
            parts.path == "/redirect" or parts.path.startswith("/redirect/")
        ):
            return "Copilot Studio"
        if any(uri == u or (u.endswith("/") and uri.startswith(u)) for u in self._extra):
            return host
        return None

    # ----- clients ---------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._seal.open("client", client_id)
        if data is None:
            return None
        uris = [str(u) for u in data.get("r", [])]
        if not uris or not all(self.platform(u) for u in uris):
            return None  # an address no longer allowed ends the registration
        method = str(data.get("m") or "client_secret_post")
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None if method == "none" else self._seal.mac(client_id),
            client_secret_expires_at=None if method == "none" else 0,
            client_id_issued_at=data.get("i"),
            redirect_uris=uris,
            token_endpoint_auth_method=method,
            grant_types=list(data.get("g") or ["authorization_code", "refresh_token"]),
            response_types=["code"],
            scope=data.get("s"),
            client_name=data.get("n"),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        platforms = {self.platform(u) for u in uris}
        if not uris or None in platforms:
            log.warning("mcp client registration refused: return address not allowed")
            raise RegistrationError(
                "invalid_redirect_uri",
                "This server accepts connections only from Claude, ChatGPT and Copilot Studio.",
            )
        # Any scope may be asked for; Entra is always asked for the search scope only.
        scopes = set((client_info.scope or "").split()) | {
            self.verifier.scope_uri,
            "offline_access",
            "openid",
            "profile",
            "email",
        }
        client_info.scope = " ".join(sorted(scopes))
        method = client_info.token_endpoint_auth_method or "client_secret_post"
        record = {
            "r": uris,
            "m": method,
            "g": list(client_info.grant_types),
            "s": client_info.scope,
            "n": (client_info.client_name or "")[:80],
            "i": client_info.client_id_issued_at,
        }
        client_info.client_id = self._seal.seal("client", record)
        client_info.client_secret = (
            None if method == "none" else self._seal.mac(client_info.client_id)
        )
        log.info("mcp client registered for %s", ", ".join(sorted(p or "" for p in platforms)))

    # ----- authorization ---------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request = {
            "c": client.client_id,
            "r": str(params.redirect_uri),
            "x": params.redirect_uri_provided_explicitly,
            "p": params.code_challenge,
            "st": params.state,
            "s": params.scopes or [],
            "rs": params.resource,
        }
        sealed = self._seal.seal("request", request, ttl=600)
        return f"{self.base}{CONSENT_PATH}?{urlencode({'request': sealed})}"

    async def consent(self, request: Request) -> Response:
        """GET: show which platform is connecting. POST: continue to Microsoft."""
        if request.method == "GET":
            sealed = request.query_params.get("request")
        else:
            form = await request.form()
            sealed = str(form.get("request") or "")
        req = self._seal.open("request", sealed)
        client = await self.get_client(str(req["c"])) if req else None
        if req is None or client is None:
            return _page(
                "This link has expired",
                "Start connecting again from Claude, ChatGPT or Copilot Studio.",
                status=400,
            )
        platform = self.platform(str(req["r"])) or "an app"
        if request.method == "GET":
            csrf = secrets.token_urlsafe(32)
            cancel = construct_redirect_uri(
                str(req["r"]), error="access_denied", state=req.get("st")
            )
            page = _consent_page(platform, str(sealed), csrf, cancel)
            page.set_cookie(
                _COOKIE, csrf, max_age=600, secure=True, httponly=True, samesite="lax", path="/"
            )
            return page
        # The POST must come from the page above in this browser: the form's token
        # has to match the cookie, which a cross-site form post can't send.
        sent = str(form.get("csrf") or "")
        if not sent or not hmac.compare_digest(sent, request.cookies.get(_COOKIE, "")):
            return _page("Please try again", "Open the connection link again.", status=400)
        verifier = secrets.token_urlsafe(48)
        state = self._seal.seal("state", {**req, "v": verifier}, ttl=900)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        to_entra = f"{self.login}/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "response_mode": "query",
                "redirect_uri": self.callback,
                "scope": self.entra_scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        response = RedirectResponse(to_entra, status_code=303)
        response.delete_cookie(_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    async def callback_route(self, request: Request) -> Response:
        """Microsoft sends the person back here; pass a sealed code on to the platform."""
        state = self._seal.open("state", request.query_params.get("state"))
        if state is None:
            return _page("This link has expired", "Start connecting again.", status=400)
        code = request.query_params.get("code")
        if not code:
            error = re.sub(r"[^a-z_]", "", request.query_params.get("error", ""))[:40]
            log.warning(
                "mcp sign-in not completed at Microsoft: %s (%s)",
                error or "no code",
                _aadsts(request.query_params.get("error_description", "")),
            )
            return RedirectResponse(
                construct_redirect_uri(
                    str(state["r"]),
                    error="access_denied",
                    error_description="Microsoft sign-in did not complete",
                    state=state.get("st"),
                ),
                status_code=302,
            )
        sealed = self._seal.seal(
            "code",
            {
                "e": code,
                "v": state["v"],
                "c": state["c"],
                "r": state["r"],
                "x": state["x"],
                "p": state["p"],
                "s": state["s"],
                "rs": state["rs"],
            },
            ttl=300,
        )
        return RedirectResponse(
            construct_redirect_uri(str(state["r"]), code=sealed, state=state.get("st")),
            status_code=302,
        )

    # ----- tokens ----------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> BrokerCode | None:
        data = self._seal.open("code", authorization_code)
        if data is None or data.get("c") != client.client_id:
            return None
        return BrokerCode(
            code=authorization_code,
            scopes=list(data.get("s") or []),
            expires_at=float(data["exp"]),
            client_id=str(data["c"]),
            code_challenge=str(data["p"]),
            redirect_uri=data["r"],
            redirect_uri_provided_explicitly=bool(data["x"]),
            resource=data.get("rs"),
            entra_code=str(data["e"]),
            entra_verifier=str(data["v"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: BrokerCode
    ) -> OAuthToken:
        tokens = await self._entra_token(
            {
                "grant_type": "authorization_code",
                "code": authorization_code.entra_code,
                "redirect_uri": self.callback,
                "code_verifier": authorization_code.entra_verifier,
            }
        )
        return await self._issue(client, tokens, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> BrokerRefreshToken | None:
        data = self._seal.open("refresh", refresh_token)
        if data is None or data.get("c") != client.client_id:
            return None
        return BrokerRefreshToken(
            token=refresh_token,
            client_id=str(data["c"]),
            scopes=list(data.get("s") or []),
            entra_refresh_token=str(data["t"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: BrokerRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        tokens = await self._entra_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token.entra_refresh_token}
        )
        return await self._issue(client, tokens, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Entra's own token, checked exactly as without the broker.
        return await self.verifier.verify_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None  # revocation is not offered; removing a person's role ends access

    async def _entra_token(self, form: dict[str, str]) -> dict[str, Any]:
        body = form | {
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "scope": self.entra_scope,
        }
        try:
            if self._http is not None:
                response = await self._http.post(f"{self.login}/token", data=body)
            else:
                async with httpx.AsyncClient(timeout=15) as http:
                    response = await http.post(f"{self.login}/token", data=body)
        except httpx.HTTPError as exc:
            log.warning("mcp sign-in: Microsoft unreachable (%s)", type(exc).__name__)
            raise TokenError("invalid_request", "Microsoft sign-in is unavailable.") from exc
        try:
            result: dict[str, Any] = response.json()
        except ValueError:
            result = {}
        if response.status_code != 200 or "access_token" not in result:
            log.warning(
                "mcp sign-in refused by Microsoft: %s (%s)",
                re.sub(r"[^a-z_]", "", str(result.get("error", "")))[:40] or response.status_code,
                _aadsts(str(result.get("error_description", ""))),
            )
            raise TokenError("invalid_grant", "Microsoft refused the sign-in. Connect again.")
        return result

    async def _issue(
        self, client: OAuthClientInformationFull, tokens: dict[str, Any], scopes: list[str]
    ) -> OAuthToken:
        access = await asyncio.to_thread(self.verifier.verify, str(tokens["access_token"]))
        if access is None or not {READER_ALL, READER_OWN} & set(access.roles):
            log.warning("mcp sign-in refused: the account has no reader role")
            raise TokenError("invalid_grant", "This account isn't allowed to use FS Email.")
        refresh = tokens.get("refresh_token")
        return OAuthToken(
            access_token=str(tokens["access_token"]),
            token_type="Bearer",  # noqa: S106 (a token type, not a password)
            expires_in=int(tokens.get("expires_in") or 3600),
            refresh_token=self._seal.seal(
                "refresh", {"t": refresh, "c": client.client_id, "s": scopes}
            )
            if refresh
            else None,
            scope=" ".join(scopes) or None,
        )


# ----- pages -------------------------------------------------------------------

_STYLE = (
    "body{margin:0;font:15px/1.5 -apple-system,'Segoe UI',Roboto,Arial,sans-serif;"
    "background:#f7f7f8;color:#0d0d0d;display:grid;place-items:center;min-height:100vh}"
    ".card{background:#fff;border:1px solid #ececf1;border-radius:16px;padding:32px;"
    "max-width:420px;margin:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}"
    "h1{font-size:20px;margin:0 0 8px}p{margin:0 0 16px;color:#3c3c43}"
    ".row{display:flex;gap:10px;flex-wrap:wrap}"
    "button,a.btn{font:inherit;font-weight:600;border-radius:10px;padding:10px 16px;"
    "border:1px solid #d9d9e3;background:#fff;color:#0d0d0d;text-decoration:none;cursor:pointer}"
    "button{background:#0d0d0d;color:#fff;border-color:#0d0d0d}"
    ".small{font-size:13px;color:#6e6e80}"
)
_HEADERS = {
    # Nothing but this page's own form; it can't be framed (no clickjacking).
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self' https://login.microsoftonline.com; frame-ancestors 'none'; "
    "base-uri 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _html(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main class=card>{body}</main></body></html>"
    )
    return HTMLResponse(page, status_code=status, headers=_HEADERS)


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    return _html(title, f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>", status)


def _consent_page(platform: str, sealed: str, csrf: str, cancel: str) -> HTMLResponse:
    name = html.escape(platform)
    return _html(
        "Connect FS Email",
        f"<h1>Connect {name} to FS Email?</h1>"
        f"<p>{name} will be able to search Finishes company email that your account "
        "is allowed to read. You sign in with your Microsoft 365 account next.</p>"
        f'<form method=post action="{CONSENT_PATH}">'
        f'<input type=hidden name=request value="{html.escape(sealed)}">'
        f'<input type=hidden name=csrf value="{html.escape(csrf)}">'
        "<div class=row><button type=submit>Continue with Microsoft</button>"
        f'<a class=btn href="{html.escape(cancel)}">Cancel</a></div></form>'
        "<p class=small style=margin-top:16px>Only people the administrator has "
        "added can sign in. If you didn't start this, close this page.</p>",
    )
