"""Deterministic verbatim chunking, so every recalled fragment is checkable against its source."""

import hashlib
import re

from .ports import Chunk

MAX_CHUNK_CHARS = 600
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n\s*\n")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _spans(content: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for found in _BOUNDARY.finditer(content):
        spans.append((start, found.start()))
        start = found.end()
    spans.append((start, len(content)))
    return [(a, b) for a, b in spans if content[a:b].strip()]


def _cut(content: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Split one over-long sentence at whitespace; never inside a word when avoidable."""
    pieces: list[tuple[int, int]] = []
    while end - start > limit:
        cut = content.rfind(" ", start + 1, start + limit)
        cut = start + limit if cut <= start else cut
        pieces.append((start, cut))
        start = cut
        while start < end and content[start].isspace():
            start += 1
    pieces.append((start, end))
    return [(a, b) for a, b in pieces if content[a:b].strip()]


def chunk_text(content: str, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    if max_chars < 32:
        raise ValueError("CHUNK_SIZE")
    pieces: list[tuple[int, int]] = []
    for start, end in _spans(content):
        pieces.extend(
            _cut(content, start, end, max_chars) if end - start > max_chars else [(start, end)]
        )
    packed: list[tuple[int, int]] = []
    for start, end in pieces:
        if packed and end - packed[-1][0] <= max_chars:
            packed[-1] = (packed[-1][0], end)
        else:
            packed.append((start, end))
    return [
        Chunk(number, start, end, content[start:end], text_hash(content[start:end]))
        for number, (start, end) in enumerate(packed)
    ]
