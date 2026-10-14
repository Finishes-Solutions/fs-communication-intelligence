"""What a push to main redeploys (scripts/deploy_targets.py, docs/DEPLOYMENT.md)."""

import subprocess
from pathlib import Path

import pytest
from deploy_targets import APPS, changed_paths, read_manifest, targets

MANIFESTS = {app: read_manifest(app) for app in APPS}
NOTHING = {"ingestion": False, "search": False, "migrations": False}


def decide(*paths: str) -> dict[str, bool]:
    return targets(paths, MANIFESTS)


@pytest.mark.parametrize(
    "path",
    [
        "docs/DEPLOYMENT.md",
        "README.md",
        "CLAUDE.md",
        "tests/unit/test_settings.py",
        "tests/fixtures/email/invoice.json",
        "db/tests/0001_core_checks.sql",
        "config/runtime.example.env",
        ".github/workflows/ci.yml",
        "scripts/bootstrap-azure.sh",
    ],
)
def test_changes_that_deploy_nothing(path: str) -> None:
    assert decide(path) == NOTHING


def test_app_folder_redeploys_only_that_app() -> None:
    assert decide("apps/search/function_app.py") == {**NOTHING, "search": True}
    assert decide("apps/ingestion/host.json") == {**NOTHING, "ingestion": True}


def test_shared_package_redeploys_every_app_that_bundles_it() -> None:
    assert decide("packages/ci_core/settings.py") == {**NOTHING, "ingestion": True, "search": True}


def test_ingestion_only_package_leaves_search_alone() -> None:
    assert decide("packages/ci_email/graph.py") == {**NOTHING, "ingestion": True}


def test_retrieval_package_redeploys_search_only() -> None:
    assert decide("packages/ci_retrieval/query.py") == {**NOTHING, "search": True}


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "scripts/build_function_app.py",
        ".github/workflows/deploy-dev.yml",
    ],
)
def test_build_inputs_redeploy_both_apps_but_not_migrations(path: str) -> None:
    assert decide(path) == {**NOTHING, "ingestion": True, "search": True}


def test_migrations_run_only_for_migration_changes() -> None:
    assert decide("db/migrations/0003_next.sql") == {**NOTHING, "migrations": True}
    assert decide("db/migrate.py") == {**NOTHING, "migrations": True}


def test_prefix_match_is_by_folder_not_by_name() -> None:
    # "apps/search-old/" must not count as "apps/search/".
    assert decide("apps/search-old/x.py") == NOTHING


def test_no_base_means_deploy_everything() -> None:
    assert changed_paths("", "HEAD") is None
    assert changed_paths("0" * 40, "HEAD") is None
    assert changed_paths("f" * 40, "HEAD") is None  # commit not in history


def test_base_equal_to_head_changes_nothing() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    ).stdout.strip()
    assert changed_paths(head, head) == []
