"""Cheap Russian-tolerant relevance: shared stem prefix, no embeddings and no second memory.

A query word matches a text word when they share a long enough common prefix, so «зонтиками»
finds «зонтики» and «предпочитает» finds «предпочитаю». It only ranks and selects what is shown;
validation, rights and citations never depend on it.
"""

import re

MIN_WORD = 4
_WORD = re.compile(r"[^\W_]{4,}")


def words(text: str) -> frozenset[str]:
    return frozenset(w.replace("ё", "е") for w in _WORD.findall(text.lower()))


def related(left: str, right: str) -> bool:
    shared = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        shared += 1
    return shared >= max(MIN_WORD, -(-min(len(left), len(right)) * 7 // 10))


def score(text: str, query: frozenset[str]) -> int:
    """How many distinct query words occur in the text, allowing endings to differ."""
    found = words(text)
    return sum(1 for word in query if any(related(word, other) for other in found))
