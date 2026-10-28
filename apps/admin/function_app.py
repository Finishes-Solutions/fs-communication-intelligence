"""Admin console Function App: the web console at https://<app>.azurewebsites.net/.

Anonymous at the Functions level on purpose: the static page holds no data,
and every /api call is refused without an Entra token carrying the
Communications.Console scope and the Communications.Admin app role
(ci_admin.api). This app's managed identity has database roles ci_operator
(schedule, scope, run history) and ci_search (published mail only, filtered by
the caller's own grants), read access to stored attachment files, and the
in-tenant Azure AI services for the classification lab. It has no mailbox
access and cannot write mail content.
"""

import azure.functions as func

from ci_admin.api import build_admin_app
from ci_cli.backends import embedder
from ci_core.settings import load_settings

_settings = load_settings()
app = func.AsgiFunctionApp(
    app=build_admin_app(_settings, embedder(_settings, interactive=True)),
    http_auth_level=func.AuthLevel.ANONYMOUS,
)
