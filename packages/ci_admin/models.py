"""Choosing the Azure OpenAI chat model in the admin console, and comparing models' costs.

The deployments come from Azure Resource Manager (the console's identity needs
the read-only Reader role on the Azure OpenAI account; scripts/azure/6-cost-
dashboard.sh). A model is switched only after it classified a synthetic test
email (no real mail is sent). Creating deployments stays in the Azure portal.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from ci_admin.billing import MANAGEMENT_SCOPE
from ci_admin.costs import DISPOSITIONS
from ci_core.db import Connection
from ci_core.operator import OperatorError, audit
from ci_core.ops import DEPLOYMENT_NAME
from ci_processing.classification import ClassificationInput, Classifier

API_VERSION = "2024-10-01"
CACHE_SECONDS = 10 * 60
# Until there is usage to measure, a classified email is assumed to send about
# 3,300 tokens and receive about 300 (the prompt, the capped body and attachments).
ASSUMED_INPUT_TOKENS = 3300
ASSUMED_OUTPUT_TOKENS = 300
# Unverified list prices per 1M tokens (input, output), standard global
# deployments; Data Zone deployments cost about 10% more. Shown only as a
# starting point until a price is saved for the deployment.
SUGGESTED_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o4-mini": (1.10, 4.40),
}
TEST_EMAIL = ClassificationInput(
    subject="Invoice 1001 for Lot 4 framing",
    body_text="Please review the attached invoice and confirm approval by Friday. Thanks, Pat",
    sender="billing@vendor.example",
    to=("owner@example.com",),
)


def account_name(endpoint: str | None) -> str | None:
    """aoai-fs-ci-dev-1026 from https://aoai-fs-ci-dev-1026.openai.azure.com/."""
    host = urlparse(endpoint or "").hostname or ""
    return host.split(".", 1)[0] or None


class DeploymentLister:
    def __init__(
        self,
        subscription_id: str,
        resource_group: str,
        account: str,
        *,
        credential: Any = None,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if credential is None:
            from ci_core.azure_auth import default_credential

            credential = default_credential()
        self._credential = credential
        self._http = client or httpx.Client(timeout=30.0)
        self._url = (
            f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/"
            f"{resource_group}/providers/Microsoft.CognitiveServices/accounts/{account}"
            f"/deployments?api-version={API_VERSION}"
        )
        self._clock = clock
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def deployments(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if self._cache and not refresh and now - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            try:
                token = self._credential.get_token(MANAGEMENT_SCOPE).token
                response = self._http.get(self._url, headers={"Authorization": f"Bearer {token}"})
            except Exception as exc:  # network or credential: a code, never a crash
                return {"available": False, "error": type(exc).__name__[:60]}
            if response.status_code >= 400:
                code = ""
                try:
                    code = str(response.json().get("error", {}).get("code", ""))
                except ValueError, AttributeError:
                    code = ""
                return {"available": False, "error": f"HTTP {response.status_code} {code}".strip()}
            result = {"available": True, "deployments": parse_deployments(response.json())}
            self._cache = (now, result)
            return result


def parse_deployments(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Chat-capable deployments (embedding models are not offered)."""
    out = []
    for d in body.get("value", []):
        props = d.get("properties") or {}
        model = props.get("model") or {}
        name = str(model.get("name") or "")
        if name.startswith("text-embedding") or "embedding" in name:
            continue
        sku = d.get("sku") or {}
        out.append(
            {
                "deployment": d.get("name"),
                "model": name,
                "version": model.get("version"),
                "sku": sku.get("name"),
                "capacity": sku.get("capacity"),
                "state": props.get("provisioningState"),
            }
        )
    return sorted(out, key=lambda x: str(x["deployment"]))


def try_model(classifier: Classifier) -> dict[str, Any]:
    """Classify the synthetic test email; refuse the model if that fails."""
    started = time.monotonic()
    try:
        result = classifier.classify(TEST_EMAIL)
    except Exception as exc:  # the service's status code, never content
        raise OperatorError(f"the model could not classify a test email ({exc})") from exc
    answered = sum(p is not None for p in result.probabilities.values())
    if not answered:
        raise OperatorError("the model answered, but not in the expected format")
    return {"seconds": round(time.monotonic() - started, 1), "labels_answered": answered}


def usage_by_model(conn: Connection, time_zone: str) -> dict[str, Any]:
    """This month's chat usage per deployment, and the average tokens per email."""
    rows = conn.execute(
        "SELECT model, sum(calls) AS calls, sum(input_tokens) AS input_tokens,"
        " sum(output_tokens) AS output_tokens, sum(estimated_usd) AS usd"
        " FROM ops_usage_cost WHERE meter = 'openai_chat' AND recorded_at >="
        " date_trunc('month', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s GROUP BY model",
        {"tz": time_zone},
    ).fetchall()
    recent = conn.execute(
        "SELECT coalesce(sum(c.input_tokens), 0) AS input_tokens,"
        " coalesce(sum(c.output_tokens), 0) AS output_tokens FROM ops_usage_cost c"
        " WHERE c.meter = 'openai_chat' AND c.source = 'processing'"
        " AND c.recorded_at > now() - interval '30 days'"
    ).fetchone()
    emails = sum(
        sum(int((r["counts"] or {}).get(k) or 0) for k in DISPOSITIONS)
        for r in conn.execute(
            "SELECT r.counts FROM ops_runs r WHERE r.kind = 'process'"
            " AND r.started_at > now() - interval '30 days'"
            " AND EXISTS (SELECT 1 FROM ai_usage u WHERE u.run_id = r.id"
            "  AND u.meter = 'openai_chat')"
        ).fetchall()
    )
    measured = bool(emails and recent and recent["input_tokens"])
    return {
        "by_model": [
            {
                "deployment": str(r["model"]).removeprefix("azure-openai:"),
                "calls": int(r["calls"]),
                "input_tokens": int(r["input_tokens"]),
                "output_tokens": int(r["output_tokens"]),
                "usd": round(float(r["usd"] or 0), 4),
            }
            for r in rows
        ],
        "per_email": {
            "measured": measured,
            "emails": emails,
            "input_tokens": round(recent["input_tokens"] / emails)
            if measured and recent
            else ASSUMED_INPUT_TOKENS,
            "output_tokens": round(recent["output_tokens"] / emails)
            if measured and recent
            else ASSUMED_OUTPUT_TOKENS,
        },
    }


def model_prices(conn: Connection) -> list[dict[str, Any]]:
    return [
        {
            **r,
            "input_usd_per_1m": float(r["input_usd_per_1m"]),
            "output_usd_per_1m": float(r["output_usd_per_1m"]),
            "updated_at": r["updated_at"].isoformat(),
        }
        for r in conn.execute(
            "SELECT deployment, input_usd_per_1m, output_usd_per_1m, verified, updated_at,"
            " updated_by FROM model_prices ORDER BY deployment"
        ).fetchall()
    ]


def set_model_price(
    conn: Connection, actor: str, deployment: str, body: dict[str, Any]
) -> list[dict[str, Any]]:
    if not DEPLOYMENT_NAME.match(deployment or ""):
        raise OperatorError("a deployment name is letters, digits, '.', '_' or '-'")
    try:
        price_in = float(body["input_usd_per_1m"])
        price_out = float(body["output_usd_per_1m"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OperatorError("give input_usd_per_1m and output_usd_per_1m as numbers") from exc
    if not (0 <= price_in < 1000 and 0 <= price_out < 1000):
        raise OperatorError("prices per 1M tokens must be between 0 and 1,000")
    verified = bool(body.get("verified", False))
    conn.execute(
        "INSERT INTO model_prices (deployment, input_usd_per_1m, output_usd_per_1m, verified,"
        " updated_by) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (deployment) DO UPDATE SET"
        " input_usd_per_1m = EXCLUDED.input_usd_per_1m,"
        " output_usd_per_1m = EXCLUDED.output_usd_per_1m, verified = EXCLUDED.verified,"
        " updated_by = EXCLUDED.updated_by, updated_at = now()",
        (deployment, price_in, price_out, verified, actor),
    )
    audit(
        conn,
        actor,
        "model_price_change",
        "model_prices",
        deployment,
        {"input_usd_per_1m": price_in, "output_usd_per_1m": price_out, "verified": verified},
    )
    return model_prices(conn)
