"""The Blob Storage adapter for original attachment files, against a mocked service."""

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ci_processing.originals import (
    AzureBlobStore,
    DirectoryStore,
    OriginalStoreError,
    blob_name,
)

DIGEST = "ab" + "0" * 62


class Token:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        assert scopes == ("https://storage.azure.com/.default",)
        return SimpleNamespace(token="t0ken", expires_on=10**10)  # noqa: S106


def store(handler: Any) -> AzureBlobStore:
    return AzureBlobStore(
        "stfscievidev1026",
        credential=Token(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )


def test_blob_names_are_content_addressed_and_validated() -> None:
    assert blob_name(DIGEST) == f"sha256/ab/{DIGEST}"
    for bad in ("../x", DIGEST.upper(), DIGEST[:-1]):
        with pytest.raises(ValueError):
            blob_name(bad)


def test_put_never_overwrites_and_accepts_already_stored() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201 if len(seen) == 1 else 409)

    s = store(handler)
    s.put(DIGEST, b"%PDF-1.4", "application/pdf")
    s.put(DIGEST, b"%PDF-1.4", "application/pdf")
    first = seen[0]
    assert first.method == "PUT"
    assert str(first.url) == (
        f"https://stfscievidev1026.blob.core.windows.net/attachments/sha256/ab/{DIGEST}"
    )
    assert first.headers["If-None-Match"] == "*"
    assert first.headers["x-ms-blob-type"] == "BlockBlob"
    assert first.headers["Authorization"] == "Bearer t0ken"


def test_get_retries_throttling_and_maps_missing_to_none() -> None:
    replies = iter([httpx.Response(503), httpx.Response(200, content=b"data"), httpx.Response(404)])
    s = store(lambda request: next(replies))
    assert s.get(DIGEST) == b"data"
    assert s.get(DIGEST) is None


def test_errors_carry_codes_only() -> None:
    s = store(lambda r: httpx.Response(403, headers={"x-ms-error-code": "AuthorizationFailure"}))
    with pytest.raises(OriginalStoreError, match="403 AuthorizationFailure"):
        s.put(DIGEST, b"x", None)


def test_directory_store_round_trip(tmp_path: Any) -> None:
    s = DirectoryStore(tmp_path)
    assert s.get(DIGEST) is None
    s.put(DIGEST, b"hello", "text/plain")
    assert s.get(DIGEST) == b"hello"
