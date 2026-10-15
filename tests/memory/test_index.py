"""The derived index alone: scope filtering, vector hygiene, expiry and safe queries."""

from datetime import timedelta

import pytest
from ai_agent_adapters.recall import SQLiteRecallIndex, match_expression
from ai_agent_application.memory import chunk_text
from ai_agent_application.memory.ports import ClaimLink, IndexedChunk, IndexedSource
from memory_support import NOW, FakeEmbedder, make_scope, run

A, B = make_scope("persona-a"), make_scope("persona-b")
A_TEST = make_scope("persona-a", "test")
OTHER_WORKSPACE = make_scope("persona-a", workspace="other-workspace")
SIG = "fake-stem/1/32"


async def entry(text, source_id="s1", version=1, expires=None, embedder=None):
    chunks = chunk_text(text)
    vectors = await embedder.embed([c.text for c in chunks]) if embedder else [None] * len(chunks)
    return IndexedSource(
        source_id,
        version,
        expires,
        tuple(
            IndexedChunk(c, tuple(v) if v else None) for c, v in zip(chunks, vectors, strict=True)
        ),
    )


async def find(index, scope, query, embedding=None, signature=None, now=NOW):
    return await index.search(scope, query, embedding, signature, limit=16, now=now)


def test_match_expression_neutralises_operators():
    assert match_expression("что где когда") is None  # only stopwords
    expression = match_expression('кошка" OR NEAR(1 2) AND * -x')
    assert expression is not None and "NEAR" not in expression.replace('"near"', "")
    assert all(part.startswith('"') and part.endswith('"') for part in expression.split(" OR "))
    assert len(match_expression(" ".join(f"слово{n}" for n in range(100))).split(" OR ")) == 24


def test_every_search_is_confined_to_its_scope(tmp_path):
    async def scenario():
        index = SQLiteRecallIndex(tmp_path / "i.sqlite")
        for scope in (A, B, A_TEST, OTHER_WORKSPACE):
            await index.put_source(scope, await entry("Общий текст про кошку"), None)
        for scope in (A, B, A_TEST, OTHER_WORKSPACE):
            found = await find(index, scope, "кошку текст")
            assert len(found.lexical) == 1
        await index.remove_source(A, "s1")
        assert (await find(index, A, "текст")).lexical == ()
        assert len((await find(index, B, "текст")).lexical) == 1  # neighbours untouched
        index.close()

    run(scenario())


def test_vectors_need_matching_signature_and_dimension(tmp_path):
    async def scenario():
        embedder = FakeEmbedder()
        index = SQLiteRecallIndex(tmp_path / "i.sqlite", dimension=32)
        assert index.vector_available
        await index.put_source(A, await entry("Кошка спит у окна", embedder=embedder), SIG)
        query = (await embedder.embed(["кошки"]))[0]
        hit = await find(index, A, "что-то иное", query, SIG)
        assert hit.vector_used and hit.vector_complete and len(hit.vector) == 1
        other_model = await find(index, A, "что-то иное", query, "other-model/1/32")
        assert not other_model.vector_used and other_model.vector == ()
        # A second model's vectors are refused for a scope already labelled with the first.
        await index.put_source(
            A, await entry("Вторая запись", "s2", embedder=embedder), "other-model/1/32"
        )
        again = await find(index, A, "что-то иное", query, SIG)
        assert again.vector_used and not again.vector_complete  # s2 is text-only, and says so
        index.close()
        # Reopening with another dimension discards vectors (never mixes dimensions).
        wider = SQLiteRecallIndex(tmp_path / "i.sqlite", dimension=64)
        outcome = await find(wider, A, "окна", [0.1] * 64, SIG)
        assert not outcome.vector_used and len(outcome.lexical) == 1
        wider.close()

    run(scenario())


def test_without_the_extension_lexical_search_still_works(tmp_path):
    async def scenario():
        index = SQLiteRecallIndex(tmp_path / "i.sqlite", dimension=None)
        assert not index.vector_available
        await index.put_source(A, await entry("Дождь и зонтик"), SIG)
        outcome = await find(index, A, "зонтик", [0.0] * 32, SIG)
        assert len(outcome.lexical) == 1 and not outcome.vector_used
        index.close()

    run(scenario())


def test_expired_entries_are_hidden_then_purged(tmp_path):
    async def scenario():
        index = SQLiteRecallIndex(tmp_path / "i.sqlite")
        soon = NOW + timedelta(days=1)
        await index.put_source(A, await entry("Срочная заметка", "s1", expires=soon), None)
        await index.put_source(A, await entry("Бессрочная заметка", "s2"), None)
        assert len((await find(index, A, "заметка", now=NOW)).lexical) == 2
        later = NOW + timedelta(days=2)
        assert [c.source_id for c in (await find(index, A, "заметка", now=later)).lexical] == ["s2"]
        assert await index.purge_expired(later) == 1
        assert set(await index.inventory(A)) == {"s2"}
        await index.compact()
        index.close()

    run(scenario())


def test_replace_scope_is_atomic_and_leaves_other_scopes(tmp_path):
    async def scenario():
        embedder = FakeEmbedder()
        index = SQLiteRecallIndex(tmp_path / "i.sqlite", dimension=32)
        await index.put_source(A, await entry("Старый текст", "old"), None)
        await index.put_source(B, await entry("Чужой текст", "b1"), None)
        good = await entry("Новый текст", "new", embedder=embedder)
        bad = IndexedSource("bad", 1, None, (IndexedChunk(chunk_text("новый текст")[0], (1.0,)),))
        with pytest.raises(ValueError):
            await index.replace_scope(A, [good, bad], [], SIG)
        assert set(await index.inventory(A)) == {"old"}  # failed rebuild changed nothing
        await index.replace_scope(A, [good], [], SIG)
        assert set(await index.inventory(A)) == {"new"}
        assert set(await index.inventory(B)) == {"b1"}
        index.close()

    run(scenario())


def test_groups_and_claim_conflicts(tmp_path):
    async def scenario():
        index = SQLiteRecallIndex(tmp_path / "i.sqlite")
        await index.put_source(A, await entry("Исходная запись", "s1"), None)
        await index.put_source(A, await entry("Пересказ записи", "s2"), None)
        await index.set_group(A, ["s1", "s2", "ghost"], "episode:ep-9")
        await index.set_group(A, ["s1"], "episode:ep-1")  # smaller id wins
        await index.set_group(A, ["s1"], "episode:ep-5")  # larger id does not
        groups = {
            c.source_id: c.group_key for c in (await find(index, A, "запись пересказ")).lexical
        }
        assert groups == {"s1": "episode:ep-1", "s2": "episode:ep-9"}
        await index.put_claim(A, ClaimLink("c1", ("s1",), ("c2",)))
        await index.put_claim(A, ClaimLink("c3", ("s1",), ()))  # no conflict: not reported
        await index.put_claim(B, ClaimLink("cx", ("s1",), ("cy",)))
        assert await index.conflicts_for(A, ["s1", "s2"]) == {"s1": ("c1",)}
        index.close()

    run(scenario())


def test_fts_put_rebuild_and_search_work_without_optional_vector_package(tmp_path, monkeypatch):
    from ai_agent_adapters.recall import index as module

    def unavailable():
        raise ModuleNotFoundError("optional vector package is absent")

    monkeypatch.setattr(module, "_sqlite_vec", unavailable)

    async def scenario():
        index = SQLiteRecallIndex(tmp_path / "fts-only.sqlite")
        try:
            first = await entry("Синтетическая заметка о синем зонтике")
            await index.put_source(A, first, None)
            assert len((await find(index, A, "зонтике")).lexical) == 1
            await index.replace_scope(A, [first], [], None)
            assert len((await find(index, A, "заметка")).lexical) == 1
            await index.remove_source(A, first.source_id)
            assert (await find(index, A, "заметка")).lexical == ()
        finally:
            index.close()

    run(scenario())
