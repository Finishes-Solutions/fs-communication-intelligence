"""The console's Connections page: the values each AI platform needs, no secrets."""

from ci_admin.connections import connection_settings
from ci_core.settings import Settings

TENANT = "11111111-1111-1111-1111-111111111111"


def settings(**overrides: object) -> Settings:
    return Settings(
        deployment_environment="development",
        attachment_unsupported_policy="publish_partial",
        search_api_app_id_uri="api://22222222-2222-2222-2222-222222222222",
        **overrides,  # type: ignore[arg-type]
    )


def test_values_come_from_settings() -> None:
    page = connection_settings(
        settings(
            search_public_url="https://srch.example.net/",
            mcp_client_id_claude="claude-id",
            mcp_client_id_chatgpt="chatgpt-id",
            mcp_client_id_copilot="copilot-id",
        ),
        TENANT,
    )
    assert page["mcp_url"] == "https://srch.example.net/mcp"
    assert page["authorization_url"] == (
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize"
    )
    assert page["token_url"] == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert page["scope"] == (
        "api://22222222-2222-2222-2222-222222222222/Communications.Search offline_access"
    )
    assert page["resource"] == "api://22222222-2222-2222-2222-222222222222"
    assert [(c["key"], c["client_id"]) for c in page["clients"]] == [
        ("claude", "claude-id"),
        ("chatgpt", "chatgpt-id"),
        ("copilot", "copilot-id"),
    ]
    assert page["missing"] == []
    assert page["quick_connect"] is False
    assert not any("secret" in k for k in page) and not any(
        "secret" in k for c in page["clients"] for k in c
    )


def test_unset_values_are_listed_as_missing() -> None:
    page = connection_settings(settings(mcp_client_id_claude="claude-id"), TENANT)
    assert page["mcp_url"] is None
    assert page["missing"] == [
        "SEARCH_PUBLIC_URL",
        "MCP_CLIENT_ID_CHATGPT",
        "MCP_CLIENT_ID_COPILOT",
    ]


def test_quick_connect_follows_the_broker_setting() -> None:
    page = connection_settings(settings(mcp_broker_client_id="broker-id"), TENANT)
    assert page["quick_connect"] is True


def test_every_sidebar_page_is_served_and_the_platform_logos_exist() -> None:
    """A page in the console's sidebar must also be a server path, or reloading it
    (or opening a link to it) shows Not Found, as /connections did."""
    import re

    from ci_admin.api import PAGES, STATIC

    app = (STATIC / "app.js").read_text()
    nav = app[app.index("const NAV = [") : app.index("];", app.index("const NAV = ["))]
    keys = re.findall(r'\["([a-z]+)", "[^"]+", "[a-z]+"\]', nav)
    assert "connections" in keys and len(keys) >= 10
    assert set(keys) <= set(PAGES)
    for name in ("claude", "chatgpt"):
        logo = (STATIC / "logos" / f"{name}.svg").read_text()
        assert logo.startswith("<svg") and "<script" not in logo
    copilot = (STATIC / "logos" / "copilot-studio.png").read_bytes()
    assert copilot.startswith(b"\x89PNG\r\n\x1a\n") and len(copilot) < 50_000
    for logo in re.findall(r'"(/logos/[a-z-]+\.(?:svg|png))"', app):
        assert (STATIC / logo.lstrip("/")).is_file(), logo
