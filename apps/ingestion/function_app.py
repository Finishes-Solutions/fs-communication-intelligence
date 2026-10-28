"""Ingestion Function App: email and Fireflies capture on a schedule, and processing drains.

Every timer ticks every 5 minutes. A capture tick asks its schedule in the
database (email: ``ci schedule`` / the admin console; Fireflies: ``ci fireflies
schedule``) whether a capture is due or was requested ("run now"); a processing
tick drains what was captured. App settings are hard kill switches:
CAPTURE_ENABLED / PROCESSING_ENABLED for email, FIREFLIES_ENABLED for both
Fireflies timers. The database mode (`ci mode set paused`) stops everything.
See docs/DEPLOY_AZURE.md and docs/FIREFLIES_PIPELINE.md.
"""

import logging

import azure.functions as func

from ci_cli.scheduled import fireflies_processing, fireflies_tick, run_processing, scheduler_tick
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


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def fireflies_capture(timer: func.TimerRequest) -> None:
    decision = fireflies_tick(load_settings())
    logging.info("fireflies capture tick: due=%s reason=%s", decision.due, decision.reason)


@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", use_monitor=True)
def fireflies_process(timer: func.TimerRequest) -> None:
    result = fireflies_processing(load_settings())
    logging.info("fireflies processing finished: %s", result)
