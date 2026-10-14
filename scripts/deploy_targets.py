"""Decide what the dev deploy must update, from the files changed since the last good deploy.

Usage (in .github/workflows/deploy-dev.yml):

    python3 scripts/deploy_targets.py --base <sha or empty> --head <sha> [--all]

Prints and, under GitHub Actions, writes to $GITHUB_OUTPUT:

    ingestion=true|false   search=true|false   migrations=true|false

Rules (docs/DEPLOYMENT.md):
- an app redeploys when apps/<app>/, a package in apps/<app>/packages.txt, the
  lock file, the build script or the deploy workflows change;
- migrations run when db/migrations/ or db/migrate.py change;
- docs, tests, fixtures and local tooling deploy nothing;
- no usable base (first deploy, rewritten history) or --all deploys everything.

Standard library only, so it runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS = ("ingestion", "search")

# Changes here can alter every app's package or how it is deployed.
SHARED_APP_INPUTS = (
    "pyproject.toml",
    "uv.lock",
    "scripts/build_function_app.py",
    ".github/workflows/deploy-dev.yml",
    ".github/workflows/deploy-function-app.yml",
)
MIGRATION_INPUTS = ("db/migrations/", "db/migrate.py")


def read_manifest(app: str, repo: Path = REPO) -> list[str]:
    """Package names listed in apps/<app>/packages.txt (comments and blanks skipped)."""
    lines = (repo / "apps" / app / "packages.txt").read_text().splitlines()
    names = [line.split("#", 1)[0].strip() for line in lines]
    return [n for n in names if n]


def _matches(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes)


def targets(changed: Iterable[str], manifests: dict[str, list[str]]) -> dict[str, bool]:
    """Pure decision: which targets the changed paths require."""
    paths = [p.strip() for p in changed if p.strip()]
    result: dict[str, bool] = {}
    for app in APPS:
        inputs = [f"apps/{app}/", *SHARED_APP_INPUTS]
        inputs += [f"packages/{name}/" for name in manifests[app]]
        result[app] = any(_matches(p, inputs) for p in paths)
    result["migrations"] = any(_matches(p, MIGRATION_INPUTS) for p in paths)
    return result


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def changed_paths(base: str, head: str) -> list[str] | None:
    """Files changed from base to head, or None when base is unusable."""
    if not base or set(base) == {"0"}:
        return None
    if _git("merge-base", "--is-ancestor", base, head).returncode != 0:
        return None  # unknown commit or rewritten history
    diff = _git("diff", "--name-only", base, head)
    if diff.returncode != 0:
        return None
    return diff.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="", help="commit of the last successful deploy")
    parser.add_argument("--head", default="HEAD", help="commit being deployed")
    parser.add_argument("--all", action="store_true", help="deploy everything")
    args = parser.parse_args(argv)

    paths = None if args.all else changed_paths(args.base, args.head)
    if paths is None:
        reason = "--all requested" if args.all else f"no usable base ({args.base or 'none'})"
        print(f"Deploying everything: {reason}")
        decision = {name: True for name in (*APPS, "migrations")}
    else:
        print(f"{len(paths)} file(s) changed since {args.base[:12]}")
        decision = targets(paths, {app: read_manifest(app) for app in APPS})

    lines = [f"{name}={'true' if value else 'false'}" for name, value in decision.items()]
    print("\n".join(lines))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
