"""Ingestion Function App: capture on a schedule, and a processing drain.

Both timers tick every 5 minutes. The capture tick asks the schedule in the
database (set from the admin console or `ci schedule`) whether a capture is due
or was requested ("run now"); the processing tick drains captured mail. The app
settings CAPTURE_ENABLED and PROCESSING_ENABLED remain hard kill switches, and
the database mode (`ci mode set paused`) stops everything. See docs/DEPLOY_AZURE.md.
"""

import logging

import azure.functions as func

from ci_cli.scheduled import run_processing, scheduler_tick
from ci_core.settings import load_settings

app = func.FunctionApp()


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def capture(timer: func.TimerRequest) -> None:
    decision = scheduler_tick(load_settings())
    logging.info("capture tick: due=%s reason=%s", decision.due, decision.reason)


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def process(timer: func.TimerRequest) -> None:
    result = run_processing(load_settings())
    logging.info("processing finished: %s", result)
