"""Paragraph chunking (CLAUDE.md section 6.8): about 350 tokens, at most 500,
with a small overlap. Tokens are approximated as 4 characters, which is close
enough for English email and avoids a tokenizer dependency.

Transcripts (meetings) are chunked by sentence windows instead, keeping the
segment range and times so each chunk can be cited (:func:`chunk_segments`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

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


SEGMENT_CHUNKER_VERSION = "segments-v1-1400c"


@dataclass(frozen=True)
class Segment:
    """One transcript sentence: provider order, speaker label as given, times in ms."""

    seq: int
    speaker: str | None
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(frozen=True)
class SegmentChunk:
    segment_from: int
    segment_to: int
    start_ms: int | None
    end_ms: int | None
    speakers: tuple[str, ...]
    text: str  # "Speaker: words" lines, consecutive sentences of one speaker joined


def _lines(segments: Sequence[Segment]) -> str:
    lines: list[str] = []
    last: str | None = None
    for s in segments:
        speaker = s.speaker or "Unknown speaker"
        if lines and speaker == last:
            lines[-1] += " " + s.text
        else:
            lines.append(f"{speaker}: {s.text}")
        last = speaker
    return "\n".join(lines)


def chunk_segments(segments: Sequence[Segment]) -> list[SegmentChunk]:
    """Windows of whole sentences, about TARGET_CHARS long, never splitting a sentence
    (one longer than MAX_CHARS becomes its own window). Each window after the first
    repeats the last sentences of the previous one, up to OVERLAP_CHARS."""
    usable = [s for s in segments if s.text.strip()]
    windows: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for s in usable:
        length = len(s.text) + len(s.speaker or "") + 3
        if current and size + length > TARGET_CHARS:
            windows.append(current)
            overlap: list[Segment] = []
            kept = 0
            for prev in reversed(current):
                kept += len(prev.text) + len(prev.speaker or "") + 3
                if kept > OVERLAP_CHARS:
                    break
                overlap.insert(0, prev)
            current, size = overlap, sum(len(o.text) + len(o.speaker or "") + 3 for o in overlap)
        current.append(s)
        size += length
    if current:
        windows.append(current)
    out: list[SegmentChunk] = []
    for w in windows:
        starts = [s.start_ms for s in w if s.start_ms is not None]
        ends = [s.end_ms for s in w if s.end_ms is not None]
        speakers = tuple(dict.fromkeys(s.speaker for s in w if s.speaker))
        out.append(
            SegmentChunk(
                w[0].seq,
                w[-1].seq,
                min(starts) if starts else None,
                max(ends) if ends else None,
                speakers,
                _lines(w),
            )
        )
    return out
