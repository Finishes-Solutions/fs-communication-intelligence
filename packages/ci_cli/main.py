"""``ci``: operator and local-development commands.

The database comes from the standard ``PG*`` environment variables; locally,
``make`` targets load them from ``local/local.env``. Every change is written to
``audit_events`` under ``--actor`` (default: ``CI_OPERATOR`` or the OS user).

    ci seed                                   load the synthetic fixture tenant (local only)
    ci tenant init                            register the real tenant (GRAPH_TENANT_ID)
    ci inventory sync                         re-read mailboxes from the Graph backend
    ci scope list
    ci scope include ADDRESS [--dataset test|production] [--revoke]
    ci scope include --all-enrolled           approve every reachable mailbox for test
    ci scope exclude ADDRESS --reason CODE    keep a whole mailbox out
    ci scope exclude --participant ADDRESS|@DOMAIN --reason CODE
                                              hide a person's or domain's mail in every mailbox
    ci scope exclude ... --revoke
    ci mode show
    ci mode set test|production|paused [--note TEXT]
    ci grant add --me | --user OBJECT_ID      let a person read the enrolled corpus
    ci grant list | ci grant revoke --user OBJECT_ID

    ci capture --hours 24                     store mail received in the last N hours
    ci process [--limit N] [--retry-failed]   extract, screen, classify, embed, publish
    ci stats                                  counts by state, outcome, label, extraction
    ci search "QUERY" [--mailbox ADDRESS]     authorized search as the signed-in person
    ci actionable [--person ADDRESS] [--hours 24] [--out FILE.md]
    ci mcp                                    MCP server over stdio for Claude

    ci schedule show                          when the ingestion app captures (also in the console)
    ci schedule set [--on|--off] [--every 60] [--window 2] [--hours 6-20]
                    [--weekdays-only|--every-day] [--time-zone America/Chicago]
                    [--processing-on|--processing-off]
    ci run-capture [--hours 24]               capture at the next 5-minute tick

Production changes (``--dataset production``, ``mode set production``) also
need ``--confirm-production``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import psycopg

from ci_cli import pipeline
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import Connection, connect
from ci_core.operator import (
    OperatorError,
    approve_mailbox,
    exclude_mailbox,
    exclude_participant,
    list_mailboxes,
    list_participant_exclusions,
    set_mode,
)
from ci_core.runtime import RuntimeConfigError, active_config, eligible_mailboxes
from ci_core.settings import DeploymentEnvironment, SettingsError, load_settings
from ci_retrieval.identity import IdentityError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci", description=__doc__.split("\n\n")[0])
    parser.add_argument("--actor", default=os.environ.get("CI_OPERATOR") or getpass.getuser())
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="load the synthetic fixture tenant (local only)")
    tenant = sub.add_parser("tenant").add_subparsers(dest="action", required=True)
    tenant.add_parser("init", help="register the real tenant (GRAPH_TENANT_ID)")

    inventory = sub.add_parser("inventory").add_subparsers(dest="action", required=True)
    inventory.add_parser("sync", help="re-read mailboxes from the Graph backend")

    scope = sub.add_parser("scope").add_subparsers(dest="action", required=True)
    scope.add_parser("list")
    include = scope.add_parser("include", help="approve a mailbox for a dataset")
    include_target = include.add_mutually_exclusive_group(required=True)
    include_target.add_argument("address", nargs="?")
    include_target.add_argument("--all-enrolled", action="store_true")
    include.add_argument("--dataset", choices=["test", "production"], default="test")
    include.add_argument("--revoke", action="store_true")
    include.add_argument("--confirm-production", action="store_true")
    exclude = scope.add_parser("exclude", help="exclude a mailbox, participant or domain")
    target = exclude.add_mutually_exclusive_group(required=True)
    target.add_argument("address", nargs="?")
    target.add_argument("--participant")
    exclude.add_argument("--reason")
    exclude.add_argument("--revoke", action="store_true")

    mode = sub.add_parser("mode").add_subparsers(dest="action", required=True)
    mode.add_parser("show")
    set_ = mode.add_parser("set")
    set_.add_argument("mode", choices=["test", "production", "paused"])
    set_.add_argument("--note")
    set_.add_argument("--confirm-production", action="store_true")

    grant = sub.add_parser("grant").add_subparsers(dest="action", required=True)
    grant.add_parser("list")
    for name in ("add", "revoke"):
        g = grant.add_parser(name)
        who = g.add_mutually_exclusive_group(required=True)
        who.add_argument("--me", action="store_true")
        who.add_argument("--user")

    capture = sub.add_parser("capture", help="store mail received in the last N hours")
    capture.add_argument("--hours", type=float, required=True)
    capture.add_argument("--workers", type=int, default=6)
    process = sub.add_parser("process", help="extract, screen, classify, embed, publish")
    process.add_argument("--limit", type=int)
    process.add_argument("--workers", type=int, default=4)
    process.add_argument("--retry-failed", action="store_true")
    sub.add_parser("stats")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--mailbox")
    search.add_argument("--limit", type=int, default=10)
    actionable = sub.add_parser("actionable")
    actionable.add_argument("--person")
    actionable.add_argument("--hours", type=float, default=24)
    actionable.add_argument("--limit", type=int, default=50)
    actionable.add_argument("--out")
    sub.add_parser("mcp", help="MCP server over stdio for Claude Desktop / Claude Code")

    schedule = sub.add_parser("schedule", help="when the ingestion app captures and processes")
    schedule_action = schedule.add_subparsers(dest="action", required=True)
    schedule_action.add_parser("show")
    sset = schedule_action.add_parser("set")
    on_off = sset.add_mutually_exclusive_group()
    on_off.add_argument("--on", dest="capture_enabled", action="store_const", const=True)
    on_off.add_argument("--off", dest="capture_enabled", action="store_const", const=False)
    sset.add_argument(
        "--every",
        dest="capture_interval_min",
        type=int,
        choices=ops.INTERVALS,
        help="minutes between captures",
    )
    sset.add_argument(
        "--window",
        dest="capture_window_hours",
        type=float,
        help="hours each capture looks back (must cover the interval)",
    )
    sset.add_argument("--hours", help="active local hours, e.g. 6-20")
    sset.add_argument("--weekdays-only", dest="weekdays_only", action="store_const", const=True)
    sset.add_argument("--every-day", dest="weekdays_only", action="store_const", const=False)
    sset.add_argument("--time-zone", dest="time_zone")
    processing = sset.add_mutually_exclusive_group()
    processing.add_argument(
        "--processing-on", dest="processing_enabled", action="store_const", const=True
    )
    processing.add_argument(
        "--processing-off", dest="processing_enabled", action="store_const", const=False
    )
    run_now = sub.add_parser("run-capture", help="ask the ingestion app to capture now")
    run_now.add_argument("--hours", type=float, help="look-back (default: the schedule's window)")
    return parser


def _require_production_confirmation(args: argparse.Namespace, production: bool) -> None:
    if production and not args.confirm_production:
        raise OperatorError("production changes need --confirm-production")


def _scope_list(conn: Connection) -> None:
    rows = list_mailboxes(conn)
    print(f"{'MAILBOX':<40} {'TYPE':<14} {'STATE':<19} TEST PROD EXCLUDED")
    for r in rows:
        print(
            f"{r.address:<40} {r.recipient_type:<14} {r.scope_state:<19} "
            f"{'yes' if r.test_approved else '-':<4} {'yes' if r.production_approved else '-':<4} "
            f"{'yes' if r.excluded else '-'}"
        )
    participants = list_participant_exclusions(conn)
    if participants:
        print("\nParticipant exclusions (hidden in every mailbox):")
        for p in participants:
            shown = (
                p["pattern"] if p["exclusion_kind"] == "participant_address" else "@" + p["pattern"]
            )
            print(f"  {shown:<40} reason={p['reason_code']}")


def _mode_show(conn: Connection) -> None:
    try:
        config = active_config(conn)
    except RuntimeConfigError as exc:
        print(f"No active mode ({exc}). Capture and search are off.")
        return
    eligible = eligible_mailboxes(conn)
    print(f"mode: {config.mode} (revision {config.revision})")
    print(f"enabled sources: {', '.join(config.enabled_providers) or 'none'}")
    print(f"mailboxes capture may read now: {len(eligible)}")
    for scope in eligible:
        print(f"  {scope.address}")
    if config.mode != "paused" and not eligible:
        print("WARNING: no mailboxes are eligible, so capture will refuse to run.")


def _seed(conn: Connection, actor: str) -> None:
    if load_settings().deployment_environment is not DeploymentEnvironment.LOCAL:
        raise OperatorError("ci seed only runs with DEPLOYMENT_ENVIRONMENT=local")
    result = seed(conn, actor)
    print(f"seeded fixture tenant: {result.inventory.total} mailboxes in inventory")
    for state, count in sorted(result.inventory.states.items()):
        print(f"  {state}: {count}")
    print("Next: ci capture --hours 24, then ci process.")


def _schedule(args: argparse.Namespace, conn: Connection, actor: str) -> None:
    if args.action == "set":
        changes = {
            k: getattr(args, k)
            for k in (
                "capture_enabled",
                "capture_interval_min",
                "capture_window_hours",
                "weekdays_only",
                "time_zone",
                "processing_enabled",
            )
            if getattr(args, k) is not None
        }
        if args.hours:
            start, _, end = args.hours.partition("-")
            try:
                changes |= {"active_start_hour": int(start), "active_end_hour": int(end)}
            except ValueError as exc:
                raise OperatorError("--hours looks like 6-20") from exc
        ops.update_schedule(conn, actor, **changes)
    s = ops.read_schedule(conn)
    print(
        f"scheduled capture: {'on' if s.capture_enabled else 'off'}, every "
        f"{s.capture_interval_min} min, looking back {s.capture_window_hours:g} h"
    )
    print(
        f"active hours: {s.active_start_hour}:00-{s.active_end_hour}:00 {s.time_zone}"
        f"{', weekdays only' if s.weekdays_only else ''}"
    )
    print(f"processing: {'on' if s.processing_enabled else 'off'}")
    print(f"last capture started: {s.last_capture_started_at or 'never'}")
    if s.run_requested_at:
        print(f"capture requested at {s.run_requested_at}; not started yet")
    print(f"now: {ops.capture_decision(s, datetime.now(UTC)).reason}")
    print("(the Azure app settings CAPTURE_ENABLED / PROCESSING_ENABLED must also be true)")


def _run(args: argparse.Namespace, conn: Connection) -> None:
    actor = args.actor
    if args.command == "seed":
        _seed(conn, actor)
        return
    if args.command == "tenant":
        pipeline.tenant_init(conn, actor)
        return
    if args.command == "inventory":
        pipeline.inventory_sync(conn, actor)
        return
    if args.command == "grant":
        pipeline.grant(conn, args, actor)
        return

    if args.command == "scope":
        if args.action == "list":
            _scope_list(conn)
        elif args.action == "include" and args.all_enrolled:
            pipeline.include_all_enrolled(conn, actor)
        elif args.action == "include":
            _require_production_confirmation(args, args.dataset == "production")
            approve_mailbox(conn, args.address, args.dataset, actor, revoke=args.revoke)
            verb = "removed approval for" if args.revoke else "approved"
            print(f"{verb} {args.address} ({args.dataset})")
        elif args.participant:
            exclude_participant(
                conn, args.participant, actor, reason=args.reason, revoke=args.revoke
            )
            print(
                f"{'revoked exclusion of' if args.revoke else 'excluded'} {args.participant}"
                " in every mailbox"
            )
        else:
            exclude_mailbox(conn, args.address, actor, reason=args.reason, revoke=args.revoke)
            print(f"{'revoked exclusion of' if args.revoke else 'excluded'} mailbox {args.address}")
        return

    if args.command == "schedule":
        _schedule(args, conn, actor)
        return
    if args.command == "run-capture":
        ops.request_run(conn, actor, args.hours)
        print("capture requested; the ingestion app starts it within about 5 minutes")
        return

    if args.action == "show":
        _mode_show(conn)
        return
    _require_production_confirmation(args, args.mode == "production")
    config = set_mode(conn, args.mode, actor, note=args.note)
    print(f"mode is now {config.mode} (revision {config.revision})")


_STANDALONE = {
    "capture": pipeline.capture,
    "process": pipeline.process,
    "search": pipeline.search,
    "actionable": pipeline.actionable,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in _STANDALONE:
            _STANDALONE[args.command](args)
        elif args.command == "stats":
            pipeline.stats()
        elif args.command == "mcp":
            pipeline.mcp()
        else:
            with connect() as conn:
                _run(args, conn)
    except (OperatorError, SettingsError, RuntimeConfigError, IdentityError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(
            f"error: cannot reach the database ({exc.__class__.__name__}); "
            "is the local stack running (make dev) and are PG* variables set?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
