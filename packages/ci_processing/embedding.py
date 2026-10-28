"""Embedder interface and the deterministic fake (``EMBEDDER=fake``).

The fake uses feature hashing over lowercase word tokens, so equal text gives
equal vectors across runs and machines, and texts sharing words land closer
together. That makes local search results stable and roughly sensible. It is
not a semantic model.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FakeEmbedder:
    model = "fake-hashing-v1"

    def __init__(self, dimensions: int = 1536) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        # A constant feature keeps empty text off the zero vector, whose cosine
        # distance is undefined in pgvector.
        for token in ["\x00bias", *_TOKEN.findall(text.lower())]:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]
