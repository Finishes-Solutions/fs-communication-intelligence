"""Who is asking. Reader authority comes from this identity, never from tool arguments.

- ``AUTH_MODE=local_test`` (only allowed with ``DEPLOYMENT_ENVIRONMENT=local``,
  enforced by settings): a fixed local principal for synthetic data.
- ``AUTH_MODE=entra`` for the local MCP server and CLI: the user signed in to
  the Azure CLI on this machine. The token comes straight from Entra over TLS
  to this process, so its claims identify the person running it; reader
  authority still comes only from ``access_grants`` rows for that object id
  (and the person's own mailbox with ``Communications.Reader.Own``).

The hosted search API (PR 7) validates delegated bearer tokens for its own
audience instead; this module does not cover that.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from ci_core.settings import AuthMode, Settings

READER_ALL = "Communications.Reader.All"
READER_OWN = "Communications.Reader.Own"
# Risk-only access (Matthew, 2026-11-13): project-risk emails from every enrolled
# mailbox, through the risk tools only, showing only the fields chosen for the
# reader (ci_retrieval.risk_access), never email links or email text.
READER_RISKS = "Communications.Reader.Risks"
# The admin console's role (ci_admin.api.ADMIN_ROLE); named here so a console
# admin who also holds READER_RISKS is not narrowed to risks.
CONSOLE_ADMIN = "Communications.Admin"
_ARM_SCOPE = "https://management.azure.com/.default"


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    oid: str
    upn: str | None
    roles: tuple[str, ...] = ()

    @property
    def own_mailbox(self) -> str | None:
        return self.upn if READER_OWN in self.roles and self.upn else None

    @property
    def risk_only(self) -> bool:
        """True when READER_RISKS is this caller's only reading role. Roles add up,
        so a broader role (all mailboxes, own mailbox, console) takes precedence."""
        return READER_RISKS in self.roles and not (
            {READER_ALL, READER_OWN, CONSOLE_ADMIN} & set(self.roles)
        )


def _claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    claims: dict[str, object] = json.loads(
        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    )
    return claims


def resolve_principal(settings: Settings) -> Principal:
    if settings.auth_mode is AuthMode.LOCAL_TEST:
        roles = tuple(
            r.strip() for r in settings.local_test_principal_roles.split(",") if r.strip()
        )
        return Principal(settings.local_test_principal_oid, None, roles)
    from azure.identity import AzureCliCredential

    try:
        claims = _claims(AzureCliCredential().get_token(_ARM_SCOPE).token)
    except Exception as exc:
        raise IdentityError("sign in first: az login") from exc
    tenant = settings.graph_tenant_id
    if tenant and str(claims.get("tid", "")).lower() != tenant.lower():
        raise IdentityError("the Azure CLI is signed in to a different tenant")
    upn = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name")
    return Principal(str(claims["oid"]), str(upn).lower() if upn else None, ())
