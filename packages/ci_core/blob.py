"""Raw payload storage. Deployed: private Blob storage (not built yet). Local: a folder."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._=\-]+(/[A-Za-z0-9._=\-]+)*$")


class BlobStore(Protocol):
    def put_json(self, key: str, value: dict[str, Any]) -> None: ...
    def get_json(self, key: str) -> dict[str, Any]: ...


class LocalBlobStore:
    """Stores JSON under a local directory. For local development only."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if not _SAFE_KEY.match(key) or ".." in key.split("/"):
            raise ValueError("unsafe blob key")
        return self.root / key

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True, indent=1))
        tmp.replace(path)

    def get_json(self, key: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self._path(key).read_text())
        return value
