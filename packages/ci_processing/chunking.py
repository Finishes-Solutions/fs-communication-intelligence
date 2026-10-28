"""Paragraph chunking (CLAUDE.md section 6.8): about 350 tokens, at most 500,
with a small overlap. Tokens are approximated as 4 characters, which is close
enough for English email and avoids a tokenizer dependency.
"""

from __future__ import annotations

import re

CHUNKER_VERSION = "para-v1-1400c"
TARGET_CHARS = 1400  # ~350 tokens
MAX_CHARS = 2000  # ~500 tokens
OVERLAP_CHARS = 200


def _split_long(paragraph: str) -> list[str]:
    """Break a paragraph longer than MAX_CHARS at sentence or word boundaries."""
    pieces: list[str] = []
    rest = paragraph
    while len(rest) > MAX_CHARS:
        window = rest[:MAX_CHARS]
        cut = max(window.rfind(". "), window.rfind("\n"))
        if cut < TARGET_CHARS // 2:
            cut = window.rfind(" ")
        if cut < TARGET_CHARS // 2:
            cut = MAX_CHARS - 1
        pieces.append(rest[: cut + 1].strip())
        rest = rest[cut + 1 :]
    if rest.strip():
        pieces.append(rest.strip())
    return pieces


def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units = [piece for p in paragraphs for piece in _split_long(p)]
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + len(unit) + 2 > TARGET_CHARS:
            chunks.append(current)
            tail = current[-OVERLAP_CHARS:]
            space = tail.find(" ")
            tail = tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail
            current = tail if len(tail) + len(unit) + 2 <= MAX_CHARS else ""
        current = f"{current}\n\n{unit}" if current else unit
    if current:
        chunks.append(current)
    return chunks
