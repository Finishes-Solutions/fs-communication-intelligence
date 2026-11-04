"""Ingestion Function App: capture on a schedule, and a processing drain.

Both timers tick every 5 minutes. The capture tick asks the schedule in the
database (set from the admin console or `ci schedule`) whether a capture is due
or was requested ("run now"); the processing tick drains captured mail. The app
settings CAPTURE_ENABLED and PROCESSING_ENABLED remain hard kill switches, and
the database mode (`ci mode set paused`) stops everything. See docs/DEPLOY_AZURE.md.
"""

import json
import logging

import azure.functions as func

from ci_cli.scheduled import fireflies_tick, run_processing, scheduler_tick
from ci_core.settings import SettingsError, load_settings

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def capture(timer: func.TimerRequest) -> None:
    settings = load_settings()
    decision = scheduler_tick(settings)
    logging.info("capture tick: due=%s reason=%s", decision.due, decision.reason)
    meetings = fireflies_tick(settings)
    logging.info("fireflies tick: due=%s reason=%s", meetings.due, meetings.reason)


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def process(timer: func.TimerRequest) -> None:
    result = run_processing(load_settings())
    logging.info("processing finished: %s", result)


@app.route(route="teams-probe", auth_level=func.AuthLevel.ADMIN, methods=["GET"])
def teams_probe(req: func.HttpRequest) -> func.HttpResponse:
    """T0, the Teams access spike (docs/TEAMS_PLAN.md): can the Teams identity read chats?

    Admin auth level: only a caller holding the app's master key (printed by
    scripts/azure/10-teams-access.sh through the Azure CLI) can run it. The reply
    holds status codes, Graph error codes, counts and the token's roles; never
    message content, ids or names.
    """
    from ci_core.azure_auth import identity_credential
    from ci_teams.probe import TeamsProbe, explain

    try:
        settings = load_settings()
        settings.require("teams_client_id")
    except SettingsError as exc:
        return func.HttpResponse(f"error: {exc}", status_code=400, mimetype="text/plain")
    probe = TeamsProbe(identity_credential(settings.teams_client_id))
    try:
        report = probe.run(req.params.get("user") or None)
    finally:
        probe.close()
    logging.info("teams probe: verdict=%s roles=%d", report.verdict, len(report.token_roles))
    body = {"report": report.to_dict(), "text": explain(report)}
    return func.HttpResponse(json.dumps(body, indent=2), mimetype="application/json")
