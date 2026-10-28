"""Which Function Apps a change redeploys (scripts/deploy-plan.sh)."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy-plan.sh"


def plan(*paths: str) -> dict[str, bool]:
    out = subprocess.run(  # noqa: S603 - fixed script, test input
        ["/bin/bash", str(SCRIPT)],
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k: v == "true" for k, v in (line.split("=") for line in out.split())}


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["packages/ci_admin/static/app.js"], (False, False, True)),
        (["packages/ci_admin/api.py", "apps/admin/host.json"], (False, False, True)),
        (["apps/ingestion/function_app.py"], (True, False, False)),
        (["apps/search/host.json"], (False, True, False)),
        (["packages/ci_retrieval/search.py"], (True, True, True)),
        (["packages/ci_core/settings.py", "docs/DEPLOY_AZURE.md"], (True, True, True)),
        (["uv.lock"], (True, True, True)),
        (["scripts/build-function-app.sh"], (True, True, True)),
        (["docs/DEPLOY_AZURE.md", "CLAUDE.md", "tests/unit/test_x.py"], (False, False, False)),
        ([".github/workflows/deploy-dev.yml", "scripts/azure/inventory.sh"], (False, False, False)),
        (["ALL"], (True, True, True)),
        ([], (False, False, False)),
    ],
)
def test_only_affected_apps_deploy(paths: list[str], expected: tuple[bool, bool, bool]) -> None:
    result = plan(*paths)
    assert (result["ingestion"], result["search"], result["admin"]) == expected
