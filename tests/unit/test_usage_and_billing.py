"""The cost page's two sources: what the Azure adapters meter as they call the
services, and Azure Cost Management's billed figures (httpx MockTransport)."""

import json
import threading
from collections.abc import Callable
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from ci_admin.billing import CACHE_SECONDS, CostClient, parse_rows, query_body
from ci_processing.azure import (
    SHIELD_PROMPT,
    AzureHttp,
    AzureOpenAIClassifier,
    AzureOpenAIEmbedder,
    AzureRedactor,
    AzureScreener,
)
from ci_processing.classification import LABELS, ClassificationInput
from ci_processing.usage import (
    LANGUAGE_PII,
    OPENAI_CHAT,
    OPENAI_EMBEDDING,
    PROMPT_SHIELDS,
    Meter,
    text_records,
)

Handler = Callable[[httpx.Request], httpx.Response]


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, *scopes: str) -> SimpleNamespace:
        self.scopes.extend(scopes)
        return SimpleNamespace(token="test-token")  # noqa: S106


def azure(handler: Handler) -> AzureHttp:
    return AzureHttp(
        credential=FakeCredential(),
        sleep=lambda _: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_text_records_count_each_started_thousand_characters() -> None:
    assert [text_records(t) for t in ("", "x", "x" * 1000, "x" * 1001, "x" * 7500)] == [
        1,
        1,
        1,
        2,
        8,
    ]


def test_meter_adds_up_across_threads_and_drains() -> None:
    meter = Meter()

    def work() -> None:
        for _ in range(500):
            meter.add(OPENAI_CHAT, "m", input_tokens=2, output_tokens=1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (row,) = meter.drain()
    assert (row.calls, row.input_tokens, row.output_tokens) == (4000, 8000, 4000)
    assert meter.drain() == []


def test_adapters_meter_tokens_and_text_records() -> None:
    meter = Meter()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "analyze-text" in url:
            docs = json.loads(request.content)["analysisInput"]["documents"]
            return httpx.Response(
                200,
                json={
                    "results": {
                        "documents": [{"id": d["id"], "entities": []} for d in docs],
                        "errors": [],
                    }
                },
            )
        if "shieldPrompt" in url:
            return httpx.Response(200, json={"documentsAnalysis": [{"attackDetected": False}]})
        if "embeddings" in url:
            batch = json.loads(request.content)["input"]
            return httpx.Response(
                200,
                json={
                    "data": [{"index": i, "embedding": [0.0]} for i in range(len(batch))],
                    "usage": {"prompt_tokens": 10 * len(batch), "total_tokens": 10 * len(batch)},
                },
            )
        content = {"labels": {lb.name: 0.0 for lb in LABELS}}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 80},
            },
        )

    http = azure(handler)
    AzureScreener("https://lang", "https://safe", http, meter=meter).screen("y" * 2500)
    AzureRedactor("https://lang", http, meter=meter).redact(["z" * 6000])
    AzureOpenAIClassifier("https://aoai", "gpt", "v", http, meter=meter).classify(
        ClassificationInput("s", "b")
    )
    AzureOpenAIEmbedder("https://aoai", "emb", "v", 1, http, meter=meter).embed(["a"] * 20)
    rows = {r.meter: r for r in meter.drain()}
    assert set(rows) == {LANGUAGE_PII, PROMPT_SHIELDS, OPENAI_CHAT, OPENAI_EMBEDDING}
    # Screening PII (2,500 chars: one 5,000-char piece = 3 records) + masking (6,000 chars
    # split 5,000 + 1,000 = 5 + 1 records).
    assert rows[LANGUAGE_PII].records == 3 + 6
    assert rows[LANGUAGE_PII].characters == 2500 + 6000
    assert rows[PROMPT_SHIELDS].records == text_records(SHIELD_PROMPT + "y" * 2500)
    assert (rows[OPENAI_CHAT].input_tokens, rows[OPENAI_CHAT].output_tokens) == (1200, 80)
    assert rows[OPENAI_EMBEDDING].calls == 2 and rows[OPENAI_EMBEDDING].input_tokens == 200


def test_missing_usage_in_a_response_counts_the_call_with_zero_tokens() -> None:
    meter = Meter()
    meter.add_openai(OPENAI_CHAT, "m", {"choices": []})
    (row,) = meter.drain()
    assert (row.calls, row.input_tokens) == (1, 0)


def test_cost_rows_are_parsed_by_column_name() -> None:
    properties = {
        "columns": [
            {"name": "UsageDate", "type": "Number"},
            {"name": "ServiceName", "type": "String"},
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ],
        "rows": [[20261027, "Foundry Models", 12.5, "USD"], [20261028, "Storage", 0.02, "USD"]],
    }
    assert parse_rows(properties) == [
        {"date": "2026-10-27", "service": "Foundry Models", "cost": 12.5, "currency": "USD"},
        {"date": "2026-10-28", "service": "Storage", "cost": 0.02, "currency": "USD"},
    ]
    body = query_body(date(2026, 9, 1), date(2026, 10, 28))
    assert body["type"] == "ActualCost" and body["dataset"]["granularity"] == "Daily"
    assert body["timePeriod"]["from"] == "2026-09-01T00:00:00Z"


def _cost_page(rows: list[list[object]], next_link: str | None = None) -> dict[str, object]:
    return {
        "properties": {
            "nextLink": next_link,
            "columns": [
                {"name": "Cost"},
                {"name": "UsageDate"},
                {"name": "ServiceName"},
                {"name": "Currency"},
            ],
            "rows": rows,
        }
    }


def client(handler: Handler, clock: Callable[[], float] = lambda: 1000.0) -> CostClient:
    return CostClient(
        "sub-test",
        "rg-test",
        credential=FakeCredential(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
        today=lambda: date(2026, 10, 28),
    )


def test_billed_costs_follow_pages_and_are_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer test-token"
        if "page2" in str(request.url):
            return httpx.Response(200, json=_cost_page([[1.0, 20261028, "Storage", "USD"]]))
        assert "/subscriptions/sub-test/resourceGroups/rg-test/" in str(request.url)
        return httpx.Response(
            200,
            json=_cost_page(
                [[5.0, 20261027, "Foundry Models", "USD"]], "https://management.azure.com/page2"
            ),
        )

    now = [1000.0]
    costs = client(handler, lambda: now[0])
    first = costs.costs()
    assert first["available"] is True and [r["cost"] for r in first["rows"]] == [5.0, 1.0]
    assert costs.costs() is first and len(calls) == 2  # cached
    now[0] += CACHE_SECONDS + 1
    costs.costs()
    assert len(calls) == 4


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (403, {"error": {"code": "AuthorizationFailed"}}, "HTTP 403 AuthorizationFailed"),
        (429, {}, "HTTP 429"),
    ],
)
def test_billed_costs_report_a_code_and_keep_the_last_good_figures(
    status: int, body: dict[str, object], error: str
) -> None:
    failing = client(lambda r: httpx.Response(status, json=body))
    assert failing.costs() == {"available": False, "error": error}

    answers = [httpx.Response(200, json=_cost_page([[2.0, 20261028, "Storage", "USD"]]))]
    flaky = client(lambda r: answers.pop(0) if answers else httpx.Response(status, json=body))
    good = flaky.costs()
    stale = flaky.costs(refresh=True)
    assert stale["rows"] == good["rows"] and stale["stale_error"] == error
