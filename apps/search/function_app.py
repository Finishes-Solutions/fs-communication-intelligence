"""Search Function App: authorized REST and MCP retrieval.

Only the health check exists so far. Search arrives in PR 7, after the hosting
spike decides between Functions and an ASGI host (CLAUDE.md §8).
"""

from pathlib import Path

import azure.functions as func

from ci_core.build_info import health_body

APP_ROOT = Path(__file__).resolve().parent

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Liveness and deployed version. Returns no data and reads no settings."""
    return func.HttpResponse(health_body("search", APP_ROOT), mimetype="application/json")
