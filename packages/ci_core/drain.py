"""Drain a database work queue with a few worker threads.

``one(conn)`` claims and finishes one item and returns its outcome, or
``None`` when nothing is queued. Each worker keeps one connection.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ci_core.db import Connection


def drain(
    connect: Callable[[], Connection],
    one: Callable[[Connection], str | None],
    *,
    limit: int | None = None,
    workers: int = 4,
    max_seconds: float | None = None,
) -> Counter[str]:
    """Run until nothing is queued, ``limit`` items are done, or ``max_seconds`` pass
    (checked before each claim, so a run can overrun by one item)."""
    counts: Counter[str] = Counter()
    budget = [limit if limit is not None else 10**12]
    lock = threading.Lock()
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None

    def take() -> bool:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        with lock:
            budget[0] -= 1
            return budget[0] >= 0

    def worker() -> Counter[str]:
        local: Counter[str] = Counter()
        with connect() as conn:
            while take():
                outcome = one(conn)
                if outcome is None:
                    break
                local[outcome] += 1
        return local

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(lambda _: worker(), range(max(1, workers))):
            counts.update(result)
    return counts
