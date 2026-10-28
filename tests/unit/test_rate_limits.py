"""Throttling slows processing down instead of failing messages: requests are paced to
a token budget, a 429 holds every worker for the service's Retry-After, and only a
throttle that outlasts the patience limit (or a persistent timeout) fails."""

import json
import threading
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest

from ci_processing.azure import AzureHttp, AzureOpenAIEmbedder, AzureServiceError
from ci_processing.ratelimit import RateGate, estimate_tokens

EMBED_URL = "https://aoai/openai/deployments/emb/embeddings?api-version=v"


class FakeCredential:
    def get_token(self, *scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="test-token")  # noqa: S106


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def http(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Clock | None = None,
    max_throttle_seconds: float = 300.0,
) -> AzureHttp:
    clock = clock or Clock()
    return AzureHttp(
        credential=FakeCredential(),
        sleep=clock.sleep,
        clock=clock,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_throttle_seconds=max_throttle_seconds,
    )


def ok(request: httpx.Request) -> httpx.Response:
    batch = json.loads(request.content)["input"]
    return httpx.Response(
        200, json={"data": [{"index": i, "embedding": [0.0]} for i in range(len(batch))]}
    )


def test_gate_spends_a_short_burst_then_paces_to_the_budget() -> None:
    clock = Clock()
    gate = RateGate(60_000, burst_seconds=2.0, clock=clock, sleep=clock.sleep)  # 1,000/s
    waits = [gate.wait(1000) for _ in range(6)]
    assert waits == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    clock.now += 30  # idle time is not banked beyond the burst
    assert [gate.wait(1000) for _ in range(4)] == [0.0, 0.0, 0.0, 1.0]


def test_a_hold_stops_every_caller_until_it_passes() -> None:
    clock = Clock()
    gate = RateGate(clock=clock, sleep=clock.sleep)  # no budget: only holds
    assert gate.wait() == 0.0
    gate.hold(5)
    gate.hold(2)  # a shorter hold never shortens the current one
    assert gate.wait() == 5.0
    assert gate.wait() == 0.0


def test_gate_is_shared_by_threads() -> None:
    clock = Clock()
    lock = threading.Lock()

    def sleep(seconds: float) -> None:
        with lock:
            clock.slept.append(seconds)

    gate = RateGate(60_000, burst_seconds=0.0, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=gate.wait, args=(1000,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # The clock does not move: each thread reserved the next second of budget.
    assert sorted(clock.slept) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]


def test_429_waits_for_the_services_retry_after_then_succeeds() -> None:
    answers = [
        httpx.Response(429, headers={"retry-after-ms": "1500", "Retry-After": "2"}),
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(429),
    ]
    clock = Clock()
    client = http(lambda r: answers.pop(0) if answers else ok(r), clock)
    AzureOpenAIEmbedder("https://aoai", "emb", "v", 1, client).embed(["a"])
    # retry-after-ms wins over Retry-After; with no header the backoff is exponential.
    assert clock.slept == [1.5, 7.0, 4.0]


def test_throttling_beyond_the_patience_limit_fails_with_the_code() -> None:
    calls = []

    def throttled(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": {"code": "RateLimitReached"}}
        )

    with pytest.raises(AzureServiceError) as err:
        AzureOpenAIEmbedder(
            "https://aoai", "emb", "v", 1, http(throttled, max_throttle_seconds=300)
        ).embed(["a"])
    assert str(err.value) == "embeddings HTTP 429 RateLimitReached"
    assert len(calls) == 6  # five 60-second waits fit in 300 seconds


def test_timeouts_are_retried_then_reported_without_content() -> None:
    answers: list[Exception | None] = [httpx.ReadTimeout("slow"), None]

    def flaky(request: httpx.Request) -> httpx.Response:
        exc = answers.pop(0) if answers else httpx.ReadTimeout("slow")
        if exc:
            raise exc
        return ok(request)

    clock = Clock()
    assert AzureOpenAIEmbedder("https://aoai", "emb", "v", 1, http(flaky, clock)).embed(["a"])
    assert clock.slept == [2.0]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(AzureServiceError) as err:
        AzureOpenAIEmbedder("https://aoai", "emb", "v", 1, http(down)).embed(["Secret"])
    assert str(err.value) == "embeddings ConnectError"


def test_embedder_spends_its_estimated_tokens_from_the_budget() -> None:
    clock = Clock()
    client = http(ok, clock)
    texts = ["x" * 4000] * 20  # first batch: 16 inputs of about 1,000 tokens
    AzureOpenAIEmbedder("https://aoai", "emb", "v", 1, client, tokens_per_minute=60_000).embed(
        texts
    )
    assert estimate_tokens(texts[:16]) == 16_016
    # The second batch waits for the first one's 16 seconds of budget, less the burst.
    assert clock.slept == [pytest.approx(14.016)]
    # Every call to the deployment shares the one paced gate.
    assert client.gate(EMBED_URL) is client.gate(EMBED_URL.split("?")[0] + "?api-version=x")
