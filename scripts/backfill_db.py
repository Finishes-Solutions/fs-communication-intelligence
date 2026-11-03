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
"""

from __future__ import annotations

import os
import subprocess
import sys

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


def main(action: str) -> None:
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
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
