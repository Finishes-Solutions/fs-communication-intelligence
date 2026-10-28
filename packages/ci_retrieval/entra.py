"""Validate Entra ID delegated access tokens for the hosted search API / MCP endpoint.

A token is accepted only if it is an RS256 v2.0 token signed by a current key
of the configured tenant, issued by that tenant, addressed to the search API
(client id or App ID URI), unexpired, and carries the delegated scope
``Communications.Search`` (``scp``). App-only tokens (no ``scp``) are refused:
every call acts for a signed-in person. Reader authority comes from the
token's app roles (``roles``) and the person's object id (``oid``), never from
tool arguments (CLAUDE.md L8).
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field

from ci_retrieval.identity import Principal

SEARCH_SCOPE = "Communications.Search"


class SigningKeySource(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class EntraAccessToken(AccessToken):
    """The SDK's AccessToken plus the identity the tools authorize with."""

    oid: str
    upn: str | None = None
    roles: list[str] = Field(default_factory=list)

    def principal(self) -> Principal:
        return Principal(self.oid, self.upn, tuple(self.roles))


class EntraTokenVerifier:
    def __init__(
        self,
        tenant_id: str,
        api_client_id: str,
        api_app_id_uri: str,
        keys: SigningKeySource | None = None,
    ) -> None:
        self.tenant_id = tenant_id.lower()
        self.issuer = f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"
        self.audiences = [api_client_id, api_app_id_uri]
        self.scope_uri = f"{api_app_id_uri.rstrip('/')}/{SEARCH_SCOPE}"
        self._keys = keys or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )

    def verify(self, token: str) -> EntraAccessToken | None:
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "oid", "tid"]},
                leeway=60,
            )
        except jwt.PyJWTError, ValueError:
            return None
        scopes = str(claims.get("scp", "")).split()
        if str(claims.get("tid", "")).lower() != self.tenant_id or SEARCH_SCOPE not in scopes:
            return None
        upn = claims.get("preferred_username") or claims.get("upn")
        return EntraAccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or ""),
            scopes=[self.scope_uri],
            expires_at=int(claims["exp"]),
            subject=str(claims["oid"]),
            oid=str(claims["oid"]),
            upn=str(upn).lower() if upn else None,
            roles=[str(r) for r in claims.get("roles", [])],
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        # Key lookup can fetch the JWKS over the network; keep it off the event loop.
        return await asyncio.to_thread(self.verify, token)
