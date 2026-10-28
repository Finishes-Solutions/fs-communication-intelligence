"""Original attachment files, one blob per distinct file, named by SHA-256.

Stored in the container ``attachments`` of the evidence storage account with
Entra tokens (the ingestion identity writes; the admin console's identity only
reads). Approved on 2026-10-28 without malware scanning: an exception to L11,
recorded in CLAUDE.md section 3a. Nothing here decides who may see a file; the
admin console serves one only after authorizing the message it belongs to.

Locally (``DEPLOYMENT_ENVIRONMENT=local``) files go to a directory instead.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

STORAGE_SCOPE = "https://storage.azure.com/.default"
STORAGE_API = "2023-11-03"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class OriginalStoreError(RuntimeError):
    """A store call failed. Carries status codes only."""


def blob_name(digest: str) -> str:
    if not _DIGEST.match(digest):
        raise ValueError("not a SHA-256 hex digest")
    return f"sha256/{digest[:2]}/{digest}"


class OriginalStore(Protocol):
    def put(self, digest: str, data: bytes, content_type: str | None) -> None: ...

    def get(self, digest: str) -> bytes | None: ...


class DirectoryStore:
    """Local development and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, digest: str, data: bytes, content_type: str | None) -> None:
        path = self.root / blob_name(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)

    def get(self, digest: str) -> bytes | None:
        path = self.root / blob_name(digest)
        return path.read_bytes() if path.exists() else None


class AzureBlobStore:
    def __init__(
        self,
        account: str,
        container: str = "attachments",
        *,
        credential: Any = None,
        client: httpx.Client | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if credential is None:
            from ci_core.azure_auth import default_credential

            credential = default_credential()
        self._credential = credential
        self._base = f"https://{account}.blob.core.windows.net/{container}"
        self._http = client or httpx.Client(timeout=120.0)
        self._max_retries = max_retries
        self._sleep = sleep

    def _call(
        self, method: str, digest: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            token = self._credential.get_token(STORAGE_SCOPE).token
            response = self._http.request(
                method,
                f"{self._base}/{blob_name(digest)}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-ms-version": STORAGE_API,
                    **(headers or {}),
                },
                **kwargs,
            )
            retryable = response.status_code in (408, 429, 500, 502, 503, 504)
            if retryable and attempt < self._max_retries:
                self._sleep(min(float(response.headers.get("Retry-After", 2**attempt)), 30.0))
                continue
            return response
        raise OriginalStoreError("retries exhausted")

    def put(self, digest: str, data: bytes, content_type: str | None) -> None:
        response = self._call(
            "PUT",
            digest,
            content=data,
            headers={
                "x-ms-blob-type": "BlockBlob",
                "x-ms-blob-content-type": content_type or "application/octet-stream",
                "If-None-Match": "*",  # never overwrite; same digest means same bytes
            },
        )
        if response.status_code in (201, 409):  # 409: already stored by another message
            return
        raise OriginalStoreError(f"PUT HTTP {response.status_code} {_code(response)}".strip())

    def get(self, digest: str) -> bytes | None:
        response = self._call("GET", digest)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise OriginalStoreError(f"GET HTTP {response.status_code} {_code(response)}".strip())
        return response.content


def _code(response: httpx.Response) -> str:
    return str(response.headers.get("x-ms-error-code", ""))
