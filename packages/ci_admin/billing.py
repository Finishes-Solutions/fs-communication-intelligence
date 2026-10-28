"""Azure's billed cost for the resource group, read from Cost Management.

The admin console's identity needs the read-only "Cost Management Reader" role
on the resource group (scripts/azure/6-cost-dashboard.sh). Azure updates these
figures every 8 to 24 hours, so today's spend appears tomorrow; the console
shows the metered estimate beside them. Results are cached for 30 minutes,
because the Cost Management API is strictly rate-limited.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

MANAGEMENT_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2023-11-01"
CACHE_SECONDS = 30 * 60
DAYS = 62  # this month and last
MAX_PAGES = 10


class BillingUnavailable(RuntimeError):
    """Carries a short code (HTTP status or error code), never content."""


def query_body(start: date, end: date) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
        },
    }


def parse_rows(properties: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows to {date, service, cost, currency}, by column name (their order varies)."""
    names = [c.get("name", "") for c in properties.get("columns", [])]
    index = {n.lower(): i for i, n in enumerate(names)}
    cost = next((index[k] for k in ("cost", "pretaxcost", "costusd") if k in index), None)
    day = index.get("usagedate")
    if cost is None or day is None:
        raise BillingUnavailable("unexpected_columns")
    service = index.get("servicename")
    currency = index.get("currency")
    out = []
    for row in properties.get("rows", []):
        raw = str(row[day])[:8]  # 20261027, or an ISO date-time
        parsed = (
            date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
            if raw.isdigit()
            else date.fromisoformat(str(row[day])[:10])
        )
        out.append(
            {
                "date": parsed.isoformat(),
                "service": str(row[service]) if service is not None else "All services",
                "cost": float(row[cost] or 0),
                "currency": str(row[currency]) if currency is not None else "USD",
            }
        )
    return out


class CostClient:
    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        *,
        credential: Any = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = lambda: datetime.now().date(),
    ) -> None:
        if credential is None:
            from ci_core.azure_auth import default_credential

            credential = default_credential()
        self._credential = credential
        self._http = client or httpx.Client(timeout=30.0)
        self._url = (
            f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/"
            f"{resource_group}/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
        )
        self._clock = clock
        self._today = today
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def costs(self, *, refresh: bool = False) -> dict[str, Any]:
        """Daily billed cost by service for the last two months (cached)."""
        with self._lock:
            now = self._clock()
            if self._cache and not refresh and now - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            try:
                end = self._today()
                rows = self._query(query_body(end - timedelta(days=DAYS - 1), end))
                result: dict[str, Any] = {"available": True, "rows": rows}
            except BillingUnavailable as exc:
                if self._cache:  # keep showing the last good figures
                    stale = dict(self._cache[1], stale_error=str(exc))
                    return stale
                return {"available": False, "error": str(exc)}
            result["fetched_at"] = datetime.fromtimestamp(now).astimezone().isoformat()
            self._cache = (now, result)
            return result

    def _query(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        url: str | None = self._url
        for _ in range(MAX_PAGES):
            if not url:
                break
            try:
                token = self._credential.get_token(MANAGEMENT_SCOPE).token
                response = self._http.post(
                    url, json=body, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                raise BillingUnavailable(type(exc).__name__) from exc
            except Exception as exc:  # credential errors: a code, not a crash
                raise BillingUnavailable(type(exc).__name__[:60]) from exc
            if response.status_code >= 400:
                code = ""
                with contextlib.suppress(ValueError, AttributeError):
                    code = str(response.json().get("error", {}).get("code", ""))
                raise BillingUnavailable(f"HTTP {response.status_code} {code}".strip())
            properties = response.json().get("properties", {})
            rows.extend(parse_rows(properties))
            url = properties.get("nextLink")
        return rows
