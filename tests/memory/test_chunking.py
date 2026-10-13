"""Chunks are verbatim, ordered slices so recalled text can be verified against its source."""

import pytest
from ai_agent_application.memory import chunk_text
from ai_agent_application.memory.chunking import text_hash

TEXT = (
    "Первая мысль про зонтик. Вторая мысль про обогрев дома!\n\n"
    "Третий абзац рассказывает о кошке, которая спит у окна… И ещё одно предложение?\n"
    "Конец."
)


def test_chunks_are_verbatim_slices_with_hashes():
    chunks = chunk_text(TEXT, 80)
    assert len(chunks) > 1
    for number, chunk in enumerate(chunks):
        assert chunk.no == number and TEXT[chunk.start : chunk.end] == chunk.text
        assert chunk.text_hash == text_hash(chunk.text) and chunk.text.strip()
    positions = [(c.start, c.end) for c in chunks]
    assert positions == sorted(positions) and all(
        a[1] <= b[0] for a, b in zip(positions, positions[1:], strict=False)
    )


def test_chunking_is_deterministic_and_respects_the_limit():
    assert chunk_text(TEXT, 80) == chunk_text(TEXT, 80)
    assert all(len(c.text) <= 80 for c in chunk_text(TEXT, 80))
    assert len(chunk_text(TEXT, 600)) == 1


def test_overlong_sentence_is_cut_at_whitespace():
    sentence = " ".join(["слово"] * 100)
    chunks = chunk_text(sentence, 64)
    assert all(len(c.text) <= 64 for c in chunks)
    assert " ".join(c.text for c in chunks) == sentence  # nothing lost, no word split


def test_blank_text_and_bad_limit():
    assert chunk_text("  \n\n ") == []
    with pytest.raises(ValueError):
        chunk_text(TEXT, 8)
