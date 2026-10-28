"""The real Microsoft Graph client (``GRAPH_BACKEND=graph``).

App-only access with a certificate credential. Mailbox reach comes from the
Exchange Online Application RBAC assignment (``Application Mail.Read`` over a
management scope); the app registration holds only ``User.Read.All`` in Entra
for the directory listing. Never add Entra ``Mail.Read`` (CLAUDE.md L5).

Not yet run against the Finishes tenant: it was written without access to it
or to Microsoft's documentation, so the first real run is its first test.
Errors carry status codes and ids, never message content.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ci_email.graph import (
    MESSAGE_SELECT,
    WELL_KNOWN_FOLDERS,
    DeltaPage,
    GraphError,
    ItemNotFound,
    Mailbox,
    MailboxAccessDenied,
    MailboxNotFound,
    MailFolder,
    Throttled,
)

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
PREFER = 'IdType="ImmutableId", outlook.body-content-type="text"'
RETRYABLE = {429, 500, 502, 503, 504}


class HttpGraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        certificate_path: str,
        *,
        max_retries: int = 6,
        timeout: float = 60.0,
        credential: Any = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if credential is None:
            from azure.identity import CertificateCredential

            credential = CertificateCredential(tenant_id, client_id, certificate_path)
        self._credential = credential
        self._http = client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # -- HTTP -----------------------------------------------------------------

    def _request(
        self, url: str, *, params: dict[str, str] | None = None, mailbox_call: bool = False
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            token = self._credential.get_token(GRAPH_SCOPE).token
            response = self._http.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Prefer": PREFER},
            )
            if response.status_code < 400:
                return response
            code = _error_code(response)
            if response.status_code in RETRYABLE and attempt < self._max_retries:
                wait = float(response.headers.get("Retry-After", 2 ** min(attempt, 5)))
                self._sleep(min(wait, 120.0))
                continue
            if response.status_code == 429:
                raise Throttled(float(response.headers.get("Retry-After", 60)))
            if response.status_code == 403:
                raise MailboxAccessDenied(f"403 {code}")
            if response.status_code == 404:
                if mailbox_call:
                    raise MailboxNotFound(f"404 {code}")
                raise ItemNotFound(f"404 {code}")
            raise GraphError(f"HTTP {response.status_code} {code}")
        raise GraphError("retries exhausted")

    def _pages(self, url: str, params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            body = self._request(next_url, params=params).json()
            params = None  # the next link already carries the query
            yield from body.get("value", [])
            next_url = body.get("@odata.nextLink")

    # -- GraphClient ------------------------------------------------------------

    def list_mailboxes(self) -> list[Mailbox]:
        mailboxes = []
        for user in self._pages(
            f"{GRAPH}/users",
            {"$select": "id,mail,displayName,userPrincipalName", "$top": "999"},
        ):
            if user.get("mail"):
                mailboxes.append(
                    Mailbox(
                        user["id"], user["mail"].lower(), user.get("displayName") or "", "unknown"
                    )
                )
        return mailboxes

    def list_folders(self, mailbox_id: str) -> list[MailFolder]:
        response = self._request(
            f"{GRAPH}/users/{mailbox_id}/mailFolders",
            params={"$select": "id,displayName", "$top": "250"},
            mailbox_call=True,
        )
        return [
            MailFolder(f["id"], f.get("displayName", ""), None)
            for f in response.json().get("value", [])
        ]

    def well_known_folder_ids(self, mailbox_id: str) -> dict[str, str]:
        ids = {}
        for name in WELL_KNOWN_FOLDERS:
            try:
                response = self._request(
                    f"{GRAPH}/users/{mailbox_id}/mailFolders/{name}", params={"$select": "id"}
                )
            except ItemNotFound:
                continue
            ids[name] = response.json()["id"]
        return ids

    def list_messages(self, mailbox_id: str, *, received_since: datetime) -> list[dict[str, Any]]:
        since = received_since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return list(
            self._pages(
                f"{GRAPH}/users/{mailbox_id}/messages",
                {
                    "$filter": f"receivedDateTime ge {since}",
                    "$select": MESSAGE_SELECT,
                    "$top": "50",
                },
            )
        )

    def delta(
        self,
        mailbox_id: str,
        folder_id: str,
        *,
        link: str | None = None,
        received_since: datetime | None = None,
    ) -> DeltaPage:
        raise NotImplementedError("per-folder delta capture is not built yet (PR 5)")

    def get_message(self, mailbox_id: str, message_id: str) -> dict[str, Any]:
        return dict(
            self._request(
                f"{GRAPH}/users/{mailbox_id}/messages/{message_id}",
                params={"$select": MESSAGE_SELECT},
            ).json()
        )

    def list_attachments(self, mailbox_id: str, message_id: str) -> list[dict[str, Any]]:
        return list(
            self._pages(
                f"{GRAPH}/users/{mailbox_id}/messages/{message_id}/attachments",
                {"$select": "id,name,contentType,size,isInline"},
            )
        )

    def get_attachment_bytes(self, mailbox_id: str, message_id: str, attachment_id: str) -> bytes:
        return self._request(
            f"{GRAPH}/users/{mailbox_id}/messages/{message_id}/attachments/{attachment_id}/$value"
        ).content


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("code", ""))
    except ValueError:
        return ""
