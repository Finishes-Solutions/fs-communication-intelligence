"""`ci` command for the local testkit.

uv run ci up         start the database (Docker), prepare it, open the UI
uv run ci ui         open the UI (database already running)
uv run ci run        run the whole flow once and print what happened
uv run ci reset      wipe and reseed the local database and fixture mailboxes
uv run ci status     show mode, mailboxes and queue
uv run ci down       stop the database (add --wipe to delete its data)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import psycopg

from ci_testkit.harness import REPO_ROOT, RunReport, Testkit, TestkitError, apply_testkit_env

COMPOSE = ["docker", "compose", "-f", str(REPO_ROOT / "local" / "docker-compose.yml")]
HOST = "127.0.0.1"  # never bind the testkit to other interfaces


def _print(reports: list[RunReport]) -> None:
    for r in reports:
        print(f"\n== {r.title}")
        for line in r.lines:
            print(f"   {line}")


def _compose(*args: str) -> None:
    if shutil.which("docker") is None:
        raise TestkitError(
            "Docker was not found. Install Docker Desktop and open it once:\n"
            "  brew install --cask docker-desktop"
        )
    result = subprocess.run([*COMPOSE, *args], check=False)  # noqa: S603
    if result.returncode != 0:
        raise TestkitError("docker compose failed. Is Docker Desktop running?")


def _wait_for_server(tk: Testkit, seconds: int = 60) -> None:
    admin_url = tk.database_url.rsplit("/", 1)[0] + "/postgres"
    deadline = time.monotonic() + seconds
    while True:
        try:
            with psycopg.connect(admin_url, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise TestkitError("the database did not become ready") from None
            time.sleep(1)


def _prepare(tk: Testkit) -> None:
    """Create and seed the database on first run; apply new migrations otherwise."""
    admin_url = tk.database_url.rsplit("/", 1)[0] + "/postgres"
    dbname = tk.database_url.rsplit("/", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
    if not exists or not tk.store.exists():
        _print([tk.reset()])
        return
    print(tk.migrate())
    try:
        tk.connection()
    except TestkitError:
        _print([tk.reset()])


def _serve(tk: Testkit, port: int, open_browser: bool) -> None:
    import uvicorn

    from ci_testkit.web import create_app

    url = f"http://{HOST}:{port}/"
    print(f"\nTestkit UI: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(tk, host=HOST, port=port), host=HOST, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="start the database, prepare it and open the UI")
    ui = sub.add_parser("ui", help="open the UI")
    for p in (up, ui):
        p.add_argument("--port", type=int, default=8765)
        p.add_argument("--no-browser", action="store_true")
    sub.add_parser("run", help="run the whole flow once")
    sub.add_parser("reset", help="wipe and reseed the local database and fixture mailboxes")
    sub.add_parser("status", help="mode, mailboxes and queue")
    down = sub.add_parser("down", help="stop the database")
    down.add_argument("--wipe", action="store_true", help="also delete the database volume")
    args = parser.parse_args(argv)

    apply_testkit_env()
    try:
        if args.command == "down":
            _compose("down", *(["-v"] if args.wipe else []))
            return 0
        tk = Testkit()
        if args.command == "up":
            _compose("up", "-d", "--wait", "db")
            _wait_for_server(tk)
            _prepare(tk)
            _serve(tk, args.port, not args.no_browser)
        elif args.command == "ui":
            _serve(tk, args.port, not args.no_browser)
        elif args.command == "run":
            _print(tk.run_all())
        elif args.command == "reset":
            _print([tk.reset()])
        elif args.command == "status":
            print(f"mode: {tk.mode()}")
            for m in tk.mailboxes():
                s = m["scope"]
                print(
                    f"  {m['fixture']['address']:32} state={s['scope_state'] if s else '-':18}"
                    f" approved={bool(s and s['test_approved'])!s:5} captured={m['capturable']}"
                )
            for q in tk.queue():
                print(f"  queue {q['job_type']:16} {q['state']:10} {q['n']}")
    except TestkitError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
