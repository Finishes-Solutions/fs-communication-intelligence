"""``ci``: operator and local-development commands.

The database comes from the standard ``PG*`` environment variables; locally,
``make`` targets load them from ``local/local.env``. Every change is written to
``audit_events`` under ``--actor`` (default: ``CI_OPERATOR`` or the OS user).

    ci seed                                   load the synthetic fixture tenant (local only)
    ci inventory sync                         re-read mailboxes from the Graph backend
    ci scope list
    ci scope include ADDRESS [--dataset test|production] [--revoke]
    ci scope exclude ADDRESS --reason CODE    keep a whole mailbox out
    ci scope exclude --participant ADDRESS|@DOMAIN --reason CODE
                                              hide a person's or domain's mail in every mailbox
    ci scope exclude ... --revoke
    ci mode show
    ci mode set test|production|paused [--note TEXT]

Production changes (``--dataset production``, ``mode set production``) also
need ``--confirm-production``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Sequence

import psycopg

from ci_cli.seed import CORPUS_KEY, seed
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
from ci_core.settings import DeploymentEnvironment, GraphBackend, SettingsError, load_settings
from ci_email.fixture import FixtureGraphClient
from ci_email.inventory import sync_inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci", description=__doc__.split("\n\n")[0])
    parser.add_argument("--actor", default=os.environ.get("CI_OPERATOR") or getpass.getuser())
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="load the synthetic fixture tenant (local only)")

    inventory = sub.add_parser("inventory").add_subparsers(dest="action", required=True)
    inventory.add_parser("sync", help="re-read mailboxes from the Graph backend")

    scope = sub.add_parser("scope").add_subparsers(dest="action", required=True)
    scope.add_parser("list")
    include = scope.add_parser("include", help="approve a mailbox for a dataset")
    include.add_argument("address")
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
    print("Next: messages are captured by the reconcile command (arrives in PR 5).")


def _inventory_sync(conn: Connection, actor: str) -> None:
    if load_settings().graph_backend is not GraphBackend.FIXTURE:
        raise OperatorError("the real Graph client arrives in PR 5; use GRAPH_BACKEND=fixture")
    graph = FixtureGraphClient.from_directory()
    row = conn.execute(
        "SELECT c.id::text AS connection_id, k.id::text AS corpus_id"
        " FROM source_connections c, corpora k"
        " WHERE c.provider = 'email' AND c.account_ref = %s AND k.corpus_key = %s",
        (graph.tenant.tenant_id, CORPUS_KEY),
    ).fetchone()
    if row is None:
        raise OperatorError("no fixture connection; run ci seed first")
    summary = sync_inventory(conn, graph, row["connection_id"], row["corpus_id"], actor)
    print(f"inventory: {summary.states}")


def _run(args: argparse.Namespace, conn: Connection) -> None:
    actor = args.actor
    if args.command == "seed":
        _seed(conn, actor)
        return
    if args.command == "inventory":
        _inventory_sync(conn, actor)
        return

    if args.command == "scope":
        if args.action == "list":
            _scope_list(conn)
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

    if args.action == "show":
        _mode_show(conn)
        return
    _require_production_confirmation(args, args.mode == "production")
    config = set_mode(conn, args.mode, actor, note=args.note)
    print(f"mode is now {config.mode} (revision {config.revision})")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with connect() as conn:
            _run(args, conn)
    except (OperatorError, SettingsError, RuntimeConfigError) as exc:
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
