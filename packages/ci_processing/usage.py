"""What each paid Azure AI call used, for the admin console's cost page.

Adapters add to a :class:`Meter` as they call the services: tokens for Azure
OpenAI (as the service reports them) and billable text records for Azure AI
Language and Content Safety (one record per started 1,000 characters of each
document, as both services bill). Processing drains the meter into
``ci.ai_usage`` at the end of each run. Counts only, never content.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass

# Meter names, matching ci.cost_prices keys (chat has an input and an output price).
OPENAI_CHAT = "openai_chat"
OPENAI_EMBEDDING = "openai_embedding"
LANGUAGE_PII = "language_pii"
PROMPT_SHIELDS = "prompt_shields"
RECORD_CHARS = 1_000


def text_records(text: str) -> int:
    """Billable text records for one document: each started 1,000 characters."""
    return max(1, math.ceil(len(text) / RECORD_CHARS))


@dataclass
class UsageRow:
    meter: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    characters: int = 0
    records: int = 0


class Meter:
    """Thread-safe running totals per (meter, model)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], UsageRow] = defaultdict(lambda: UsageRow("", ""))
        self._lock = threading.Lock()

    def add(
        self,
        meter: str,
        model: str,
        *,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        characters: int = 0,
        records: int = 0,
    ) -> None:
        with self._lock:
            row = self._rows[(meter, model)]
            row.meter, row.model = meter, model
            row.calls += calls
            row.input_tokens += input_tokens
            row.output_tokens += output_tokens
            row.characters += characters
            row.records += records

    def add_texts(self, meter: str, model: str, texts: list[str]) -> None:
        """One call that sent these documents to a per-record service."""
        self.add(
            meter,
            model,
            characters=sum(len(t) for t in texts),
            records=sum(text_records(t) for t in texts),
        )

    def add_openai(self, meter: str, model: str, response: dict[str, object]) -> None:
        usage = response.get("usage")
        tokens = usage if isinstance(usage, dict) else {}
        self.add(
            meter,
            model,
            input_tokens=int(tokens.get("prompt_tokens") or 0),
            output_tokens=int(tokens.get("completion_tokens") or 0),
        )

    def drain(self) -> list[UsageRow]:
        """The totals so far, and start again from zero."""
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: (r.meter, r.model))
            self._rows.clear()
        return rows
