"""Build identity of a deployed app, used by the health endpoints.

scripts/build_function_app.py writes ``build_info.json`` next to
``function_app.py``. The deploy workflow's smoke test compares its ``git_sha``
with the commit it deployed, which proves the new code is the code running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BUILD_INFO_FILE = "build_info.json"


def read_build_info(app_root: Path) -> dict[str, Any]:
    """Return the packaged build info, or ``{"git_sha": "unknown"}`` when absent."""
    path = app_root / BUILD_INFO_FILE
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {"git_sha": "unknown"}
    return data if isinstance(data, dict) else {"git_sha": "unknown"}


def health_body(app_name: str, app_root: Path) -> str:
    """JSON for a health response: status, app name and deployed commit only."""
    info = read_build_info(app_root)
    return json.dumps(
        {"status": "ok", "app": app_name, "git_sha": str(info.get("git_sha", "unknown"))}
    )
