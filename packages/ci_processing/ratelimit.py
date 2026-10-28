"""One gate per Azure deployment, shared by every worker thread that calls it.

Azure OpenAI enforces a deployment's tokens-per-minute quota over short windows
(about 10 seconds), so many workers sending large embedding batches at once get
HTTP 429 although the minute's total would fit. The gate spaces requests to a
token budget, and after any 429 it holds every caller until the service's
Retry-After has passed, instead of each worker retrying on its own.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateGate:
    """``tokens_per_minute`` None or 0: no pacing, only the shared hold after a 429.

    ``burst_seconds`` of budget may be spent at once; beyond that requests wait
    their turn (each reserves ``tokens / rate`` seconds, in the order they ask)."""

    def __init__(
        self,
        tokens_per_minute: int | None = None,
        *,
        burst_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = (tokens_per_minute or 0) / 60.0
        self._burst = burst_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0  # when the budget reserved so far is used up
        self._held_until = 0.0

    def wait(self, tokens: int = 0) -> float:
        """Block until this request may be sent; returns the seconds waited."""
        with self._lock:
            now = self._clock()
            start = max(now, self._held_until)
            if self._rate > 0:
                reserved = max(self._next, start)
                start = max(start, reserved - self._burst)
                self._next = reserved + max(tokens, 1) / self._rate
            delay = start - now
        if delay > 0:
            self._sleep(delay)
        return max(delay, 0.0)

    def hold(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` (the service said it is throttling)."""
        with self._lock:
            self._held_until = max(self._held_until, self._clock() + seconds)


def estimate_tokens(texts: list[str]) -> int:
    """About 4 characters per token for English text (an estimate, used for pacing only)."""
    return sum(len(t) for t in texts) // 4 + len(texts)
