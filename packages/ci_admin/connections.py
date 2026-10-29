"""The Connections page: what Claude, ChatGPT and Copilot Studio need to reach the
hosted MCP endpoint (docs/DEPLOY_AZURE.md §10).

IDs and URLs only. Client secrets are never stored in or shown by the console;
the page gives the command that creates one, and the person pastes it straight
into the platform.
"""

from typing import Any

from ci_core.settings import Settings
from ci_retrieval.entra import SEARCH_SCOPE

# key, title, Entra app registration (scripts/azure/2-search-api-entra.sh), app setting
PLATFORMS = (
    ("claude", "Claude", "FS Communications MCP - Claude", "MCP_CLIENT_ID_CLAUDE"),
    ("chatgpt", "ChatGPT", "FS Communications MCP - ChatGPT", "MCP_CLIENT_ID_CHATGPT"),
    (
        "copilot",
        "Copilot Studio",
        "FS Communications MCP - Copilot Studio",
        "MCP_CLIENT_ID_COPILOT",
    ),
)


def connection_settings(settings: Settings, tenant: str) -> dict[str, Any]:
    base = (settings.search_public_url or "").rstrip("/")
    api_uri = settings.search_api_app_id_uri or ""
    login = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    clients = [
        {
            "key": key,
            "title": title,
            "app_registration": app,
            "client_id": getattr(settings, setting.lower()) or None,
        }
        for key, title, app, setting in PLATFORMS
    ]
    missing = [] if base else ["SEARCH_PUBLIC_URL"]
    missing += [p[3] for p, c in zip(PLATFORMS, clients, strict=True) if not c["client_id"]]
    return {
        "mcp_url": f"{base}/mcp" if base else None,
        "tenant_id": tenant,
        "authorization_url": f"{login}/authorize",
        "token_url": f"{login}/token",
        "scope": f"{api_uri}/{SEARCH_SCOPE} offline_access",
        "resource": api_uri,
        "clients": clients,
        "missing": missing,
        # The sign-in broker (scripts/azure/8-mcp-broker.sh): platforms then need only
        # the MCP URL. Its client ID is set on this app too, so the page can tell.
        "quick_connect": bool(settings.mcp_broker_client_id),
    }
