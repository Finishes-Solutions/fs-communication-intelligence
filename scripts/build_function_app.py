"""Build a deployable Function App folder: app code, its shared packages, dependencies.

Usage:

    uv run python scripts/build_function_app.py ingestion --sha <git sha>

Produces build/<app>/ ready for ``Azure/functions-action`` (no remote build):

    function_app.py, host.json            from apps/<app>/
    ci_core/, ...                         the packages listed in apps/<app>/packages.txt
    .python_packages/lib/site-packages/   locked dependencies (Linux x86_64, CPython 3.14)
    requirements.txt                      what was installed, for the record
    build_info.json                       app name and git commit, read by /api/health

Dependencies are installed as Linux wheels for the Flex Consumption runtime even
when the build runs on a Mac, so a local build matches the CI build.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from deploy_targets import APPS, read_manifest

REPO = Path(__file__).resolve().parent.parent
PYTHON_VERSION = "3.14"  # must match the Function Apps' runtime (CLAUDE.md L3)
PLATFORM = "x86_64-manylinux_2_28"
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "local.settings.json", "packages.txt")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO)  # noqa: S603


def build(app: str, out: Path, sha: str, install: bool = True) -> Path:
    """Assemble the package; ``install=False`` skips dependencies (used by tests)."""
    if app not in APPS:
        raise SystemExit(f"unknown app {app!r}; expected one of {APPS}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(REPO / "apps" / app, out, ignore=SKIP)
    for name in read_manifest(app):
        src = REPO / "packages" / name
        if not src.is_dir():
            raise SystemExit(
                f"apps/{app}/packages.txt lists {name!r}, but packages/{name} is missing"
            )
        shutil.copytree(src, out / name, ignore=SKIP)

    if install:
        install_dependencies(out)
    info = {"app": app, "git_sha": sha, "built_at": datetime.now(UTC).isoformat(timespec="seconds")}
    (out / "build_info.json").write_text(json.dumps(info, indent=2) + "\n")
    return out


def install_dependencies(out: Path) -> None:
    requirements = out / "requirements.txt"
    run(
        [
            "uv", "export", "--frozen", "--no-dev", "--group", "functions",
            "--no-emit-project", "--no-hashes", "--format", "requirements-txt",
            "--output-file", str(requirements),
        ]
    )  # fmt: skip
    run(
        [
            "uv", "pip", "install", "--requirements", str(requirements),
            "--target", str(out / ".python_packages" / "lib" / "site-packages"),
            "--python-version", PYTHON_VERSION, "--python-platform", PLATFORM,
            "--only-binary", ":all:", "--no-config",
        ]
    )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", choices=APPS)
    parser.add_argument("--sha", default="local", help="git commit being built")
    parser.add_argument("--out", type=Path, help="output folder (default build/<app>)")
    args = parser.parse_args(argv)
    out = build(args.app, args.out or REPO / "build" / args.app, args.sha)
    print(f"built {args.app} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
