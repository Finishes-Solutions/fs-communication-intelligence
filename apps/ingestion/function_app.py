"""Ingestion Function App: hourly capture and a processing drain.

Both timers are always deployed but do nothing until switched on with app
settings (CAPTURE_ENABLED, PROCESSING_ENABLED), and both obey the database mode
(`ci mode set paused` stops everything). Schedules come from app settings:
CAPTURE_SCHEDULE (default hourly at :05) and PROCESS_SCHEDULE (every 5 minutes).
Run capture once on demand from the portal (function -> Test/Run) or the admin
endpoint; see docs/DEPLOY_AZURE.md.
"""

import logging

import azure.functions as func

from ci_cli.scheduled import run_capture, run_processing
from ci_core.settings import load_settings

app = func.FunctionApp()


@app.timer_trigger(schedule="%CAPTURE_SCHEDULE%", arg_name="timer", use_monitor=True)
def capture(timer: func.TimerRequest) -> None:
    result = run_capture(load_settings())
    logging.info("capture finished: %s", result)


@app.timer_trigger(schedule="%PROCESS_SCHEDULE%", arg_name="timer", use_monitor=True)
def process(timer: func.TimerRequest) -> None:
    result = run_processing(load_settings())
    logging.info("processing finished: %s", result)
