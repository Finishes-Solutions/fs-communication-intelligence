"""Database checks for a running backfill, from an operator's Mac (Azure CLI sign-in).

    uv run --env-file local/azure-dev.env python scripts/backfill_db.py activity
    uv run --env-file local/azure-dev.env python scripts/backfill_db.py index
    uv run --env-file local/azure-dev.env python scripts/backfill_db.py explain
    uv run --env-file local/azure-dev.env python scripts/backfill_db.py unstick

activity  connections by state and what they wait on (counts only)
index     create the processing-queue index (migration 0027) without blocking writes
explain   the plan of the processing claim query (should use that index)
unstick   mark capture runs still 'running' after their host stopped as failed, so
          the next tick requeues their backfill window
hour [HOURS]  what happened in the last HOURS (default 1): emails captured, processed
          (classified, skipped as bulk, published), failed, capture weeks done or failed,
          the queue now and the hourly rate
kill-idle [MINUTES]  end other users' sessions left idle inside a transaction for more
          than MINUTES (default 8): workers of a restarted app whose connections
          linger, still holding row locks other workers wait on. A live worker is
          idle in a transaction only while it waits on an AI call (seconds to a
          few minutes), so it is not affected at the default.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import psycopg

CLAIM = """
SELECT v.id FROM ci.communication_versions v
JOIN ci.communications c ON c.id = v.communication_id
JOIN ci.email_messages em ON em.communication_id = c.id
JOIN ci.source_scopes s ON s.id = em.mailbox_scope_id
JOIN ci.email_mailboxes m ON m.scope_id = s.id
WHERE v.processing_state = 'fetched' AND c.dataset_key = ci.active_dataset()
ORDER BY v.created_at
FOR UPDATE OF v SKIP LOCKED
LIMIT 1
"""


def connect(autocommit: bool = False) -> psycopg.Connection[tuple[object, ...]]:
    if os.environ.get("DATABASE_AUTH", "").lower() == "entra" and "PGPASSWORD" not in os.environ:
        os.environ["PGPASSWORD"] = subprocess.check_output(
            [  # noqa: S607 - the operator's Azure CLI
                "az",
                "account",
                "get-access-token",
                "--resource-type",
                "oss-rdbms",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            text=True,
        ).strip()
    return psycopg.connect(autocommit=autocommit)


def main(action: str, arg: str | None = None) -> None:
    if action == "activity":
        with connect() as c:
            rows = c.execute(
                "SELECT coalesce(state, '-') AS state, coalesce(wait_event_type, '-') AS wtype,"
                " coalesce(wait_event, '-') AS wevent, count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 15"
            ).fetchall()
            for r in rows:
                print(f"{r[3]:>5}  {r[0]:<22} {r[1]:<10} {r[2]}")
            fetched = c.execute(
                "SELECT count(*) FROM ci.communication_versions WHERE processing_state = 'fetched'"
            ).fetchone()
            print(f"\nfetched (waiting): {fetched[0] if fetched else '?'}")
    elif action == "index":
        with connect(autocommit=True) as c:
            c.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS communication_versions_fetched_queue"
                " ON ci.communication_versions (created_at) WHERE processing_state = 'fetched'"
            )
            ok = c.execute(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid"
                " WHERE x.relname = 'communication_versions_fetched_queue'"
            ).fetchone()
            print("index ready" if ok and ok[0] else "index NOT valid: drop it and run again")
    elif action == "explain":
        with connect() as c:
            for (line,) in c.execute("EXPLAIN " + CLAIM).fetchall():
                print(line)
            c.rollback()
    elif action == "unstick":
        with connect() as c:
            rows = c.execute(
                "UPDATE ci.ops_runs SET status = 'failed', finished_at = now(),"
                " error_code = 'stale_run' WHERE kind = 'capture' AND status = 'running'"
                " AND started_at < now() - interval '20 minutes' RETURNING id, started_at"
            ).fetchall()
            c.commit()
            print(f"marked {len(rows)} capture run(s) as stale: {rows}")
    elif action == "hour":
        hours = float(arg or 1)
        t = {"t": datetime.now(UTC) - timedelta(hours=hours)}
        with connect() as c:

            def one(query: str) -> int:
                row = c.execute(query, t).fetchone()
                return int(row[0]) if row and row[0] is not None else 0

            def states(query: str) -> dict[str, int]:
                return {str(r[0]): int(r[1]) for r in c.execute(query, t).fetchall()}

            captured = one(
                "SELECT count(*) FROM ci.communication_versions WHERE created_at > %(t)s"
            )
            classified = one("SELECT count(*) FROM ci.classification_runs WHERE created_at > %(t)s")
            skipped = one(
                "SELECT count(*) FROM ci.policy_decisions WHERE check_name = 'prefilter'"
                " AND decision = 'reject' AND decided_at > %(t)s"
            )
            published = one(
                "SELECT count(*) FROM ci.communication_versions WHERE published_at > %(t)s"
            )
            waiting = one(
                "SELECT count(*) FROM ci.communication_versions WHERE processing_state = 'fetched'"
            )
            failed = one(
                "SELECT count(*) FROM ci.communication_versions WHERE processing_state = 'failed'"
            )
            weeks = states(
                "SELECT state, count(*) FROM ci.ops_backfill_windows"
                " WHERE finished_at > %(t)s GROUP BY state"
            )
            left = states(
                "SELECT state, count(*) FROM ci.ops_backfill_windows"
                " WHERE state IN ('pending', 'running', 'failed') AND %(t)s IS NOT NULL"
                " GROUP BY state"
            )
            c.rollback()
        processed = classified + skipped
        rate = processed / hours if hours else 0
        print(f"last {hours:g} hour(s):")
        print(f"  captured            {captured:>9,}")
        print(f"  processed           {processed:>9,}  ({rate:,.0f} an hour)")
        print(f"    classified by AI  {classified:>9,}")
        print(f"    skipped as bulk   {skipped:>9,}")
        print(f"    published         {published:>9,}")
        print(f"  capture weeks       done {weeks.get('done', 0)}, failed {weeks.get('failed', 0)}")
        print("now:")
        print(f"  waiting to process  {waiting:>9,}")
        print(f"  failed (all time)   {failed:>9,}")
        print(
            f"  weeks left          pending {left.get('pending', 0)},"
            f" running {left.get('running', 0)}, failed {left.get('failed', 0)}"
        )
        if rate > 0:
            print(f"  the waiting queue alone takes about {waiting / rate:.1f} hours at this rate")
    elif action == "kill-idle":
        minutes = int(arg or 8)
        with connect() as c:
            rows = c.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                " AND usename <> current_user AND state = 'idle in transaction'"
                " AND now() - state_change > make_interval(mins => %s)",
                (minutes,),
            ).fetchall()
            c.commit()
            ended = sum(1 for r in rows if r[0])
            print(f"ended {ended} session(s) idle in a transaction > {minutes} min")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "", sys.argv[2] if len(sys.argv) > 2 else None)
