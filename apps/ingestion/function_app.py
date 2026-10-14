"""Ingestion Function App: capture, processing worker and maintenance timers.

Only the health check exists so far. Capture and processing arrive in PR 5 and 6.
"""

from pathlib import Path

import azure.functions as func

from ci_core.build_info import health_body

APP_ROOT = Path(__file__).resolve().parent

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Liveness and deployed version. Returns no data and reads no settings."""
    return func.HttpResponse(health_body("ingestion", APP_ROOT), mimetype="application/json")
