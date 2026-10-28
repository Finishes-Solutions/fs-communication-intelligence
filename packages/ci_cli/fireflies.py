"""``ci fireflies``: set up, run and schedule Fireflies meeting capture.

    ci fireflies init [--production --confirm-production]
                                       register the workspace (whole company) and enable the source
    ci fireflies capture --hours 72    store meetings dated in the last N hours
    ci fireflies process [--limit N] [--retry-failed]
                                       AI analysis, chunk, embed, publish
    ci fireflies stats                 meeting counts by state, and the latest runs
    ci fireflies schedule show
    ci fireflies schedule set [--on|--off] [--every 60] [--lookback 72]
                              [--processing-on|--processing-off]
    ci fireflies run-capture [--hours 72 | --since 2026-09-01]
                                       capture at the next 5-minute tick (up to 93 days)

Keep a person's meetings out with ``ci scope exclude --participant ADDRESS
--reason CODE`` (it hides every meeting and email they took part in) and one
meeting with an item exclusion.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from ci_cli import backends
from ci_cli.scheduled import fireflies_profile
from ci_core import ops
from ci_core.db import connect
from ci_core.operator import OperatorError
from ci_core.settings import load_settings
from ci_fireflies import setup
from ci_fireflies.capture import capture_meetings
from ci_fireflies.process import process_meetings, retry_failed_meetings


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    ff = sub.add_parser("fireflies", help="Fireflies meetings: setup, capture, process, schedule")
    action = ff.add_subparsers(dest="action", required=True)
    init = action.add_parser("init", help="register the workspace and enable the source")
    init.add_argument("--production", action="store_true", help="also approve for production")
    init.add_argument("--confirm-production", action="store_true")
    cap = action.add_parser("capture", help="store meetings dated in the last N hours")
    cap.add_argument("--hours", type=float, required=True)
    proc = action.add_parser("process", help="AI analysis, chunk, embed, publish")
    proc.add_argument("--limit", type=int)
    proc.add_argument("--workers", type=int, default=2)
    proc.add_argument("--retry-failed", action="store_true")
    action.add_parser("stats")
    schedule = action.add_parser("schedule").add_subparsers(dest="schedule_action", required=True)
    schedule.add_parser("show")
    sset = schedule.add_parser("set")
    on_off = sset.add_mutually_exclusive_group()
    on_off.add_argument("--on", dest="capture_enabled", action="store_const", const=True)
    on_off.add_argument("--off", dest="capture_enabled", action="store_const", const=False)
    sset.add_argument("--every", dest="capture_interval_min", type=int, choices=setup.INTERVALS)
    sset.add_argument(
        "--lookback", dest="lookback_hours", type=float, help="hours each capture looks back"
    )
    processing = sset.add_mutually_exclusive_group()
    processing.add_argument(
        "--processing-on", dest="processing_enabled", action="store_const", const=True
    )
    processing.add_argument(
        "--processing-off", dest="processing_enabled", action="store_const", const=False
    )
    run_now = action.add_parser("run-capture", help="ask the ingestion app to capture now")
    when = run_now.add_mutually_exclusive_group()
    when.add_argument("--hours", type=float, help="look-back (default: the schedule's)")
    when.add_argument("--since", help="from this date, midnight Central time (YYYY-MM-DD)")


def _schedule_show(s: setup.Schedule) -> None:
    print(
        f"scheduled Fireflies capture: {'on' if s.capture_enabled else 'off'}, every "
        f"{s.capture_interval_min} min, looking back {s.lookback_hours:g} h"
    )
    print(f"processing: {'on' if s.processing_enabled else 'off'}")
    print(f"last capture started: {s.last_capture_started_at or 'never'}")
    if s.run_requested_at:
        print(f"capture requested at {s.run_requested_at}; not started yet")
    print(f"now: {setup.capture_decision(s, datetime.now(UTC)).reason}")
    print("(the Azure app setting FIREFLIES_ENABLED must also be true)")


def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    if args.action == "capture":
        result = capture_meetings(
            lambda role: connect(role=role),
            backends.fireflies_client(settings),
            hours=args.hours,
            profile=fireflies_profile(settings),
            per_user=settings.fireflies_list_per_user,
        )
        print(f"captured: {dict(sorted(result.counts.items()))}")
        if result.listing_failed:
            raise OperatorError("every Fireflies listing call failed; check the API key")
        return
    if args.action == "process":
        if args.retry_failed:
            with connect(role="ci_ingest") as conn:
                print(f"requeued {retry_failed_meetings(conn)} failed meeting version(s)")
                conn.commit()
        if args.limit == 0:
            return
        counts = process_meetings(
            lambda: connect(role="ci_ingest"),
            backends.meeting_backends(settings),
            limit=args.limit,
            workers=args.workers,
        )
        print(f"processed: {dict(sorted(counts.items()))}")
        return
    if args.action == "stats":
        with connect(role="ci_operator") as conn:
            print("meeting versions by state:")
            for r in conn.execute(
                "SELECT processing_state, reason, meetings FROM ops_meeting_states"
                " ORDER BY meetings DESC"
            ).fetchall():
                reason = f" ({r['reason']})" if r["reason"] else ""
                print(f"  {r['processing_state']}{reason}: {r['meetings']}")
            print("latest Fireflies runs:")
            for r in conn.execute(
                "SELECT kind, status, started_at, counts, error_code FROM ops_runs"
                " WHERE kind LIKE 'fireflies%' ORDER BY started_at DESC LIMIT 5"
            ).fetchall():
                print(
                    f"  {r['started_at']:%Y-%m-%d %H:%M} {r['kind']:<18} {r['status']:<9}"
                    f" {r['error_code'] or ''} {dict(sorted(r['counts'].items()))}"
                )
        return

    with connect() as conn:
        if args.action == "init":
            if args.production and not args.confirm_production:
                raise OperatorError("production changes need --confirm-production")
            ws = setup.init_workspace(
                conn, settings.fireflies_workspace_ref, args.actor, production=args.production
            )
            conn.commit()
            print(f"Fireflies workspace registered (scope {ws.scope_id}); source enabled")
            print("Next: ci fireflies capture --hours 72, then ci fireflies process.")
        elif args.action == "schedule":
            if args.schedule_action == "set":
                changes = {
                    k: getattr(args, k)
                    for k in (
                        "capture_enabled",
                        "capture_interval_min",
                        "lookback_hours",
                        "processing_enabled",
                    )
                    if getattr(args, k) is not None
                }
                setup.update_schedule(conn, args.actor, **changes)
                conn.commit()
            _schedule_show(setup.read_schedule(conn))
        elif args.action == "run-capture":
            hours = ops.hours_since(args.since, datetime.now(UTC)) if args.since else args.hours
            setup.request_run(conn, args.actor, hours)
            conn.commit()
            looking = f" ({hours:g} hours back)" if hours else ""
            print(f"Fireflies capture requested{looking}; it starts within about 5 minutes")
