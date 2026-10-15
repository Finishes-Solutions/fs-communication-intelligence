"""Paragraph chunking (~350 tokens, max 500, small overlap) and fake embeddings.

Token counts are estimated at 4 characters per token; the real tokenizer
arrives with the Azure OpenAI embedder. The fake embedder hashes words into
1536 buckets (feature hashing), so it is deterministic and similar wording
gives similar vectors. It is not a semantic model.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from ci_core.settings import EmbedderBackend

CHUNKER_VERSION = "paragraph-350.v1"
TARGET_TOKENS, MAX_TOKENS, OVERLAP_TOKENS = 350, 500, 40


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _split_long(paragraph: str) -> list[str]:
    """Split one oversized paragraph on sentence boundaries, then hard-wrap."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    parts: list[str] = []
    current = ""
    for s in sentences:
        while estimate_tokens(s) > MAX_TOKENS:
            cut = MAX_TOKENS * 4
            parts.append(s[:cut])
            s = s[cut:]
        if current and estimate_tokens(current + " " + s) > TARGET_TOKENS:
            parts.append(current)
            current = s
        else:
            current = f"{current} {s}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paragraphs:
        pieces.extend(_split_long(p) if estimate_tokens(p) > TARGET_TOKENS else [p])
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and estimate_tokens(current + "\n\n" + piece) > TARGET_TOKENS:
            chunks.append(current)
            tail = current[-OVERLAP_TOKENS * 4 :]
            overlap = tail[tail.find(" ") + 1 :] if " " in tail else ""
            current = f"{overlap}\n\n{piece}".strip() if overlap else piece
        else:
            current = f"{current}\n\n{piece}".strip()
    if current:
        chunks.append(current)
    return chunks


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    model = "fake-feature-hash.v1"

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big")
            vec[h % self.dimensions] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            vec[0], norm = 1.0, 1.0
        return [v / norm for v in vec]


@dataclass(frozen=True)
class Chunk:
    seq: int
    display_text: str  # returned as evidence
    embedding_input: str  # header + text; the header is never shown as evidence


def make_embedder(backend: EmbedderBackend, dimensions: int) -> Embedder:
    if backend is EmbedderBackend.FAKE:
        return FakeEmbedder(dimensions)
    raise NotImplementedError("EMBEDDER=azure_openai is not built yet (PR 6); use fake locally")
