"""One shared, caching Entra credential for Azure services and Azure PostgreSQL.

On a Mac the token comes from the signed-in Azure CLI, and each ``az`` call
takes about a second, so tokens are cached until five minutes before they
expire. Inside Azure (``IDENTITY_ENDPOINT`` set) the managed identity is used;
elsewhere the managed-identity probe is skipped to avoid its timeout.
"""

from __future__ import annotations

import os
import threading
import time
from functools import lru_cache
from typing import Any

REFRESH_MARGIN_SECONDS = 300


class CachedCredential:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._tokens: dict[tuple[str, ...], Any] = {}
        self._lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        with self._lock:
            token = self._tokens.get(scopes)
            if token is None or token.expires_on - time.time() < REFRESH_MARGIN_SECONDS:
                token = self._inner.get_token(*scopes, **kwargs)
                self._tokens[scopes] = token
            return token


@lru_cache(maxsize=1)
def default_credential() -> CachedCredential:
    from azure.identity import DefaultAzureCredential

    in_azure = bool(os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"))
    return CachedCredential(
        DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
            exclude_managed_identity_credential=not in_azure,
        )
    )


@lru_cache(maxsize=4)
def identity_credential(client_id: str | None) -> CachedCredential:
    """The credential for one named user-assigned identity (``client_id``).

    Inside Azure a Function App can carry several user-assigned identities;
    ``DefaultAzureCredential`` picks the one in ``AZURE_CLIENT_ID``, so another one
    (the Teams identity) must be asked for by client id. Outside Azure, or with no
    client id, this is the shared default credential (the signed-in Azure CLI).
    """
    in_azure = bool(os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"))
    if not client_id or not in_azure:
        return default_credential()
    from azure.identity import ManagedIdentityCredential

    return CachedCredential(ManagedIdentityCredential(client_id=client_id))
