"""Search Function App: the hosted MCP endpoint at https://<app>.azurewebsites.net/mcp.

Anonymous at the Functions level on purpose: the MCP app itself refuses every
request without a valid Entra delegated token (ci_retrieval.entra) and serves
the OAuth protected-resource metadata clients use to find Entra. This app's
managed identity can only read the published_* views (database role
ci_search) and call the embedding model; it has no mailbox or write access.
"""

import azure.functions as func

from ci_cli.backends import embedder
from ci_core.settings import load_settings
from ci_retrieval.mcp_server import build_http_app

_settings = load_settings()
app = func.AsgiFunctionApp(
    app=build_http_app(_settings, embedder(_settings)),
    http_auth_level=func.AuthLevel.ANONYMOUS,
)
