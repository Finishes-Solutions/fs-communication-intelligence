"""The keyword table of migration 0040 (ci.chunk_terms), from an operator's Mac
(Azure CLI sign-in, as the login that applies migrations).

    uv run --env-file local/azure-dev.env python scripts/keyword_table.py status
    uv run --env-file local/azure-dev.env python scripts/keyword_table.py build
    uv run --env-file local/azure-dev.env python scripts/keyword_table.py prewarm
    uv run --env-file local/azure-dev.env python scripts/keyword_table.py off

status   whether keyword search reads the table, how far the copy got, sizes
build    copy every existing chunk's keyword data into the table, in batches of
         heap pages (resumable: run it again after an interruption and it goes on
         where it stopped), build its index without blocking writes, check that no
         chunk is missing, then switch keyword search over to it
prewarm  load the table and its index into memory (after a server restart or resize)
off      switch keyword search back to the chunks table (on: switch it back)

Prints counts, sizes and timings only, never email content. Processing can keep
running during the build: new chunks are added by the migration's trigger.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import psycopg

# Heap pages of ci.chunks per batch: about 5 chunks per page, so ~10,000 chunks.
BATCH_PAGES = 2000

COPY = """
INSERT INTO ci.chunk_terms (chunk_id, communication_id, version_id, is_doc, occurred_at, tsv)
SELECT ch.id, ch.communication_id, ch.version_id, ch.chunk_kind = 'attachment_text',
       c.occurred_at, ch.tsv
FROM ci.chunks ch JOIN ci.communications c ON c.id = ch.communication_id
WHERE ch.ctid >= %(lo)s::tid AND ch.ctid < %(hi)s::tid
ON CONFLICT (chunk_id) DO NOTHING
"""

MISSING = """
INSERT INTO ci.chunk_terms (chunk_id, communication_id, version_id, is_doc, occurred_at, tsv)
SELECT ch.id, ch.communication_id, ch.version_id, ch.chunk_kind = 'attachment_text',
       c.occurred_at, ch.tsv
FROM ci.chunks ch JOIN ci.communications c ON c.id = ch.communication_id
WHERE NOT EXISTS (SELECT 1 FROM ci.chunk_terms ct WHERE ct.chunk_id = ch.id)
ON CONFLICT (chunk_id) DO NOTHING
"""


def connect() -> psycopg.Connection[tuple[Any, ...]]:
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
    return psycopg.connect(autocommit=True)


def _one(conn: psycopg.Connection[tuple[Any, ...]], sql: str, *args: Any) -> Any:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def _pages(conn: psycopg.Connection[tuple[Any, ...]]) -> int:
    return int(
        _one(
            conn,
            "SELECT pg_relation_size('ci.chunks') / current_setting('block_size')::bigint",
        )
    )


def _state(conn: psycopg.Connection[tuple[Any, ...]]) -> tuple[bool, int]:
    row = conn.execute("SELECT ready, copied_through FROM ci.chunk_terms_state").fetchone()
    if row is None:
        raise SystemExit("ci.chunk_terms_state is empty: apply migration 0040 first")
    return bool(row[0]), int(row[1])


def _index_valid(conn: psycopg.Connection[tuple[Any, ...]]) -> bool | None:
    """True when chunk_terms_tsv exists and is usable, False when an interrupted
    concurrent build left it invalid, None when it does not exist."""
    return _one(
        conn,
        "SELECT i.indisvalid FROM pg_index i"
        " WHERE i.indexrelid = to_regclass('ci.chunk_terms_tsv')",
    )


def status(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    if _one(conn, "SELECT to_regclass('ci.chunk_terms_state') IS NOT NULL") is not True:
        print("migration 0040 is not applied")
        return
    ready, through = _state(conn)
    pages = _pages(conn)
    print(f"keyword search reads the table: {'yes' if ready else 'no'}")
    print(f"copy: heap pages {through + 1:,} of {pages:,}")
    index = {True: "built", False: "INVALID (run build again)", None: "not built"}
    print(f"index: {index[_index_valid(conn)]}")
    table = _one(conn, "SELECT pg_size_pretty(pg_table_size('ci.chunk_terms'))")
    gin = _one(conn, "SELECT pg_size_pretty(pg_relation_size(to_regclass('ci.chunk_terms_tsv')))")
    print(f"sizes: table {table}, index {gin or '-'}")
    limit = _one(conn, "SELECT score_limit FROM ci.chunk_terms_state")
    print(f"score limit: {limit:,} chunks per term")


def build(conn: psycopg.Connection[tuple[Any, ...]], batch_pages: int = BATCH_PAGES) -> None:
    ready, through = _state(conn)
    if ready:
        print("already done: keyword search reads the table")
        return
    start = time.monotonic()
    first = through + 1
    page = first
    pages = _pages(conn)
    print(f"copying heap pages {first:,} to {pages:,} of ci.chunks")
    last_report = start
    while page < pages:
        hi = page + batch_pages
        for attempt in (1, 2, 3):
            try:
                with conn.transaction():
                    conn.execute(COPY, {"lo": f"({page},0)", "hi": f"({hi},0)"})
                    conn.execute("UPDATE ci.chunk_terms_state SET copied_through = %s", (hi - 1,))
                break
            except psycopg.errors.ForeignKeyViolation:
                # A chunk was deleted (an email reprocessed) while it was copied.
                if attempt == 3:
                    raise
                time.sleep(2)
        page = hi
        pages = max(pages, _pages(conn))
        now = time.monotonic()
        if now - last_report >= 30 or page >= pages:
            done = page - first
            rate = done / (now - start) if now > start else 0
            left = (pages - page) / rate / 60 if rate else 0
            print(
                f"  {min(page, pages):,} / {pages:,} pages"
                f"  ({now - start:,.0f} s, about {left:,.0f} min left)",
                flush=True,
            )
            last_report = now
    valid = _index_valid(conn)
    if valid is False:
        print("dropping the index an interrupted build left behind")
        conn.execute("DROP INDEX CONCURRENTLY IF EXISTS ci.chunk_terms_tsv")
        valid = None
    if valid is None:
        print("building the index (does not block processing)...", flush=True)
        t = time.monotonic()
        conn.execute("SET maintenance_work_mem = '2GB'")
        conn.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS chunk_terms_tsv"
            " ON ci.chunk_terms USING gin (tsv)"
        )
        if _index_valid(conn) is not True:
            raise SystemExit("the index build did not finish; run build again")
        print(f"  index built in {time.monotonic() - t:,.0f} s")
    conn.execute("ANALYZE ci.chunk_terms")
    added = conn.execute(MISSING).rowcount
    if added:
        print(f"added {added:,} chunks the copy had not seen")
    missing = _one(
        conn,
        "SELECT count(*) FROM ci.chunks ch"
        " WHERE NOT EXISTS (SELECT 1 FROM ci.chunk_terms ct WHERE ct.chunk_id = ch.id)",
    )
    if missing:
        raise SystemExit(f"{missing:,} chunks are still missing; run build again")
    conn.execute("UPDATE ci.chunk_terms_state SET ready = true, ready_at = now()")
    print(f"done in {(time.monotonic() - start) / 60:,.1f} min: keyword search reads the table")


def prewarm(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    conn.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
    for name in ("ci.chunk_terms", "ci.chunk_terms_tsv"):
        t = time.monotonic()
        blocks = _one(conn, "SELECT pg_prewarm(%s::regclass)", name)
        print(f"{name}: {blocks * 8 / 1024:,.0f} MB in {time.monotonic() - t:,.0f} s", flush=True)


def switch(conn: psycopg.Connection[tuple[Any, ...]], on: bool) -> None:
    if on and _index_valid(conn) is not True:
        raise SystemExit("the index is not built: run build")
    conn.execute(
        "UPDATE ci.chunk_terms_state SET ready = %s, ready_at = CASE WHEN %s THEN now() END",
        (on, on),
    )
    print(f"keyword search reads the {'table' if on else 'chunks table (as before 0040)'}")


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    with connect() as conn:
        conn.execute("SET statement_timeout = 0")
        if command == "status":
            status(conn)
        elif command == "build":
            build(conn)
        elif command == "prewarm":
            prewarm(conn)
        elif command in ("on", "off"):
            switch(conn, command == "on")
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
