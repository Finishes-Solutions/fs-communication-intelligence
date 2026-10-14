"""Function App packaging: health endpoints, package manifests and the build layout."""

import ast
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import azure.functions as func
import pytest
from build_function_app import build
from deploy_targets import APPS, read_manifest

from ci_core.build_info import health_body

REPO = Path(__file__).resolve().parents[2]


def load_app(app: str) -> ModuleType:
    path = REPO / "apps" / app / "function_app.py"
    spec = importlib.util.spec_from_file_location(f"function_app_{app}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ci_imports(path: Path) -> set[str]:
    """Top-level ci_* packages imported by one Python file."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        found |= {n.split(".")[0] for n in names if n.startswith("ci_")}
    return found


@pytest.mark.parametrize("app", APPS)
def test_app_registers_only_health_for_now(app: str) -> None:
    module = load_app(app)
    names = [f.get_function_name() for f in module.app.get_functions()]
    assert names == ["health"]


@pytest.mark.parametrize("app", APPS)
def test_health_reports_app_and_unknown_sha_outside_a_build(app: str) -> None:
    module = load_app(app)
    response = module.health(func.HttpRequest(method="GET", url="/api/health", body=b""))
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"status": "ok", "app": app, "git_sha": "unknown"}


def test_health_body_reads_packaged_build_info(tmp_path: Path) -> None:
    (tmp_path / "build_info.json").write_text(json.dumps({"git_sha": "abc123"}))
    assert json.loads(health_body("search", tmp_path))["git_sha"] == "abc123"


def test_health_body_survives_a_corrupt_build_info(tmp_path: Path) -> None:
    (tmp_path / "build_info.json").write_text("not json")
    assert json.loads(health_body("search", tmp_path))["git_sha"] == "unknown"


@pytest.mark.parametrize("app", APPS)
def test_manifest_covers_every_ci_package_the_app_imports(app: str) -> None:
    """The deployed package must contain every ci_* package reachable from the app."""
    needed: set[str] = set()
    queue = list((REPO / "apps" / app).glob("*.py"))
    seen: set[str] = set()
    while queue:
        for name in ci_imports(queue.pop()):
            needed.add(name)
            if name not in seen:
                seen.add(name)
                queue.extend((REPO / "packages" / name).rglob("*.py"))
    missing = needed - set(read_manifest(app))
    assert not missing, f"add {sorted(missing)} to apps/{app}/packages.txt"


def test_search_package_never_contains_mailbox_code() -> None:
    """L4: the search app gets no mailbox access, so it ships no Graph mail client."""
    assert "ci_email" not in read_manifest("search")


@pytest.mark.parametrize("app", APPS)
def test_build_layout(app: str, tmp_path: Path) -> None:
    out = build(app, tmp_path / app, sha="deadbeef", install=False)
    assert (out / "function_app.py").is_file()
    assert (out / "host.json").is_file()
    assert not (out / "packages.txt").exists()
    for name in read_manifest(app):
        assert (out / name / "__init__.py").is_file()
    assert json.loads((out / "build_info.json").read_text())["git_sha"] == "deadbeef"
