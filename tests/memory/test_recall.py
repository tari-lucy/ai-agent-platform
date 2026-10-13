"""Recall: access before context, experience dedupe, FTS fallback, provenance and conflicts."""

from datetime import timedelta

from ai_agent_application.memory import (
    ClaimDraft,
    EpisodeDraft,
    MemoryService,
    RecallService,
)
from ai_agent_application.storage import RecordAccess
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.states import Provenance
from memory_support import (
    MEMORY,
    NOW,
    OPERATOR,
    FakeEmbedder,
    command,
    make_scope,
    new_world,
    run,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")
B = make_scope("persona-b")
OTHER_WORKSPACE = make_scope("persona-a", workspace="other-workspace")


def ids(result):
    return {f.source.source_id for f in result.fragments}


def ref(source, scope=A):
    return SourceRef(
        source_id=source.id, version=source.version, scope=scope, availability="available"
    )


def test_fragments_are_verbatim_attributed_and_bounded(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        text = "Дождь начался внезапно. Мы взяли зонтик и пошли к реке. Там нас ждала лодка."
        made = await world.add(
            A, text, external_id="m-1", author_id="person-7", identity_confidence=0.8
        )
        result = await world.ask(A, "зонтик лодка")
        assert result.index_state == "ready" and result.complete and ids(result) == {made.source_id}
        fragment = result.fragments[0]
        assert fragment.content in text and fragment.source.version == 1
        assert fragment.source.scope == A and fragment.source.availability == "available"
        assert "third_party_message" in fragment.attribution and "person-7" in fragment.attribution
        assert "0.80" in fragment.attribution
        # Result limit and context token budget are honoured.
        for number in range(5):
            await world.add(A, f"Заметка номер {number} про зонтик.", external_id=f"n-{number}")
        assert len((await world.ask(A, "зонтик", limit=3)).fragments) == 3
        tight = await world.ask(A, "зонтик", context_token_limit=10)
        assert tight.fragments and sum(len(f.content) for f in tight.fragments) <= 30
        assert not (await world.ask(A, "зонтик", context_token_limit=1)).complete
        await world.store.close()

    run(scenario())


def test_rights_are_checked_per_purpose_before_anything_enters_a_context(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        private = await world.add(A, "Личная заметка про кактус.", api_transfer_allowed=False)
        shared = await world.add(
            A,
            "Общая заметка про кактус.",
            api_transfer_allowed=True,
            public_disclosure_allowed=True,
        )
        locked = await world.add(A, "Закрытая заметка про кактус.", read_allowed=False)
        assert ids(await world.ask(A, "кактус")) == {private.source_id, shared.source_id}
        assert ids(await world.ask(A, "кактус", purpose="api_transfer")) == {shared.source_id}
        assert ids(await world.ask(A, "кактус", purpose="public_disclosure")) == {shared.source_id}
        assert locked.source_id not in ids(await world.ask(A, "кактус", limit=50))
        await world.store.close()

    run(scenario())


def test_revocation_after_indexing_wins_over_a_stale_index(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, "Секрет про кактус.", external_id="m-1")
        assert ids(await world.ask(A, "кактус")) == {made.source_id}
        # Rights change through a service WITHOUT the index: the index still holds the old version.
        blind = MemoryService(world.store, MEMORY, world.memory.extensions, clock=world.clock)
        await blind.set_rights(
            command(A),
            made.source_id,
            1,
            read_allowed=False,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        after = await world.ask(A, "кактус")
        assert after.fragments == [] and made.source_id in after.missing_source_ids
        assert not after.complete
        # Content edited under the index's feet (new version): stale hit is dropped, not served.
        other = await world.add(A, "Ещё запись про кактус.", external_id="m-2")
        await blind.set_rights(
            command(A),
            other.source_id,
            1,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=True,
        )
        stale = await world.ask(A, "кактус")
        assert stale.fragments == [] and stale.index_state == "lagging" and not stale.complete
        await world.memory.reconcile(A)
        assert ids(await world.ask(A, "кактус")) == {other.source_id}
        await world.store.close()

    run(scenario())


def test_deleted_and_expired_sources_are_never_served(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        blind = MemoryService(world.store, MEMORY, world.memory.extensions, clock=world.clock)
        gone = await world.add(A, "Удалённая запись про сад.", external_id="g-1")
        short = await world.add(
            A, "Недолгая запись про сад.", external_id="g-2", occurred_at=NOW - timedelta(days=29)
        )
        keep = await world.add(A, "Живая запись про сад.", external_id="g-3")
        await blind.delete_source(command(A), gone.source_id, 1)  # index not told
        assert ids(await world.ask(A, "сад")) == {short.source_id, keep.source_id}
        world.clock.advance(days=2)  # g-2 passes its 30 days; index not purged
        result = await world.ask(A, "сад")
        assert ids(result) == {keep.source_id}
        assert await world.index.purge_expired(world.clock.now) == 1
        await world.store.close()

    run(scenario())


def test_scopes_personas_workspaces_and_work_test_never_mix(tmp_path):
    async def scenario():
        scopes = [A, A_TEST, B, OTHER_WORKSPACE]
        world = await new_world(tmp_path, scopes)
        made = {}
        for scope in scopes:
            made[scope.partition] = await world.add(
                scope, "Одинаковая фраза про маяк.", external_id="same"
            )
        for scope in scopes:
            result = await world.ask(scope, "маяк")
            assert ids(result) == {made[scope.partition].source_id}
            assert all(f.source.scope == scope for f in result.fragments)
        # A scope with nothing in it finds nothing, whatever the neighbours hold.
        empty = make_scope("persona-empty")
        await world.provision(empty)
        assert (await world.ask(empty, "маяк")).fragments == []
        await world.store.close()

    run(scenario())


def test_readers_need_a_record_acl_and_get_only_the_purposes_it_grants(tmp_path):
    async def scenario():
        acl = (RecordAccess(OPERATOR.id, read=True, api_transfer=False),)
        world = await new_world(tmp_path, [A], reader_access=acl)
        made = await world.add(A, "Заметка для оператора про маяк.", external_id="m-1")
        reader = RecallService(
            world.store, OPERATOR, world.index, embedder=world.embedder, clock=world.clock
        )
        assert ids(await world.ask(A, "маяк", service=reader)) == {made.source_id}
        assert (await world.ask(A, "маяк", service=reader, purpose="api_transfer")).fragments == []
        # A reader without any record ACL gets nothing even though the index knows the text.
        bare = await new_world(tmp_path / "bare", [A])
        await bare.add(A, "Заметка без доступа про маяк.", external_id="m-1")
        stranger = RecallService(
            bare.store, OPERATOR, bare.index, embedder=bare.embedder, clock=bare.clock
        )
        assert (await bare.ask(A, "маяк", service=stranger)).fragments == []
        await world.store.close()
        await bare.store.close()

    run(scenario())


def test_one_lived_experience_counts_once_but_matching_text_does_not_merge(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        original = await world.add(A, "Мы нашли странный камень у реки.", external_id="e-1")
        retold = await world.add(
            A, "Позже я рассказал про странный камень у реки.", external_id="e-2"
        )
        stranger = await world.add(
            A, "Мы нашли странный камень у реки.", external_id="e-3", author_id="person-9"
        )
        before = await world.ask(A, "странный камень", limit=8)
        assert ids(before) == {original.source_id, retold.source_id, stranger.source_id}
        sources = [await world.memory.get_source(A, r.source_id) for r in (original, retold)]
        await world.memory.record_episode(
            command(A),
            EpisodeDraft(
                sources=tuple(ref(s) for s in sources),
                participant_ids=("person-1",),
                choice="Рассмотреть камень",
                actual_outcome="Камень обычный",
                operation_id="operation-1",
                provenance="observation",
            ),
        )
        after = await world.ask(A, "странный камень", limit=8)
        returned = ids(after)
        assert len(returned) == 2 and stranger.source_id in returned  # episode counted once
        assert len(returned & {original.source_id, retold.source_id}) == 1
        # The grouping survives a full rebuild from authority.
        await world.memory.reindex_scope(A)
        assert len(ids(await world.ask(A, "странный камень", limit=8))) == 2
        await world.store.close()

    run(scenario())


def test_fts_fallback_is_used_and_reported_when_embeddings_fail(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        cat = await world.add(A, "Кошка спит у окна.", external_id="c-1")
        await world.add(A, "Зонтик остался дома.", external_id="c-2")
        healthy = await world.ask(A, "кошки у окна")  # stem-vectors find the inflected word
        assert healthy.index_state == "ready" and healthy.complete and cat.source_id in ids(healthy)

        world.embedder.fail = True
        fallback = await world.ask(A, "кошки у окна")
        assert fallback.index_state == "degraded" and not fallback.complete
        assert ids(fallback) == {cat.source_id}  # shared word "окна": lexical still answers

        no_inflection = await world.ask(A, "кошки")
        assert no_inflection.fragments == []  # exact-token FTS cannot bridge "кошки"/"Кошка"
        assert no_inflection.index_state == "degraded"
        world.embedder.fail = False
        assert cat.source_id in ids(await world.ask(A, "кошки"))  # vectors restore it
        await world.store.close()

    run(scenario())


def test_without_an_embedder_the_system_is_lexical_and_says_so(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A], embedder=None)
        made = await world.add(A, "Обогрев включили вечером.", external_id="h-1")
        result = await world.ask(A, "обогрев")
        assert ids(result) == {made.source_id}
        assert result.index_state == "degraded" and not result.complete
        stop_words = await world.ask(A, "что где когда")
        assert stop_words.fragments == [] and stop_words.index_state == "degraded"
        await world.store.close()

    run(scenario())


def test_embedder_model_change_never_mixes_vectors_until_reindexed(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, "Кошка спит у окна.", external_id="c-1")
        assert made.source_id in ids(await world.ask(A, "кошки"))
        successor = FakeEmbedder(signature="fake-stem/2/32")
        world.recall_service.embedder = world.memory.embedder = successor
        stale = await world.ask(A, "кошки")
        assert stale.index_state == "degraded" and stale.fragments == []  # old vectors ignored
        await world.memory.reindex_scope(A)
        fresh = await world.ask(A, "кошки")
        assert fresh.index_state == "ready" and made.source_id in ids(fresh)
        await world.store.close()

    run(scenario())


def test_broken_or_missing_index_is_unavailable_not_silently_empty(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.add(A, "Запись про маяк.", external_id="m-1")
        world.index.close()
        result = await world.ask(A, "маяк")
        assert (
            result.index_state == "unavailable" and result.fragments == [] and not result.complete
        )
        await world.store.close()

    run(scenario())


def test_conflicts_and_origin_are_reported_with_the_fragment(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        said = await world.add(A, "Игрок сказал, что замок построил он.", external_id="k-1")
        other = await world.add(
            A,
            "Другой игрок сказал, что замок построила она.",
            external_id="k-2",
            author_id="person-2",
        )
        mine = await world.add(
            A,
            "Я видел замок возле холма.",
            external_id="k-3",
            provenance=Provenance.OBSERVATION,
        )
        s1 = await world.memory.get_source(A, said.source_id)
        s2 = await world.memory.get_source(A, other.source_id)
        await world.memory.record_claim(
            command(A),
            ClaimDraft("claim-1", "subject-1", "Построил он", "reported", (ref(s1),), 0.4),
        )
        await world.memory.record_claim(
            command(A),
            ClaimDraft(
                "claim-2", "subject-1", "Построила она", "reported", (ref(s2),), 0.4, ("claim-1",)
            ),
        )
        result = await world.ask(A, "замок построил построила", limit=8)
        by_source = {f.source.source_id: f for f in result.fragments}
        assert by_source[said.source_id].conflicts == ["claim-1"]
        assert by_source[other.source_id].conflicts == ["claim-2"]
        assert "third_party_message" in by_source[said.source_id].attribution
        observed = (await world.ask(A, "холма")).fragments[0]
        assert "observation" in observed.attribution and observed.conflicts == []
        # A claim the reader cannot see is not advertised through the conflict list.
        reader = RecallService(
            world.store, OPERATOR, world.index, embedder=world.embedder, clock=world.clock
        )
        assert mine.source_id  # operator holds no record ACL here, so nothing is visible
        assert (await world.ask(A, "замок", service=reader)).fragments == []
        await world.store.close()

    run(scenario())


def test_reconcile_repairs_missing_stale_and_extra_entries(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        keep = await world.add(A, "Остаётся про маяк.", external_id="r-1")
        lost = await world.add(A, "Потерялось про маяк.", external_id="r-2")
        await world.index.remove_source(A, lost.source_id)  # simulate index loss
        ghost = await world.add(A, "Призрак про маяк.", external_id="r-3")
        await MemoryService(
            world.store, MEMORY, world.memory.extensions, clock=world.clock
        ).delete_source(command(A), ghost.source_id, 1)
        report = await world.memory.reconcile(A)
        assert (
            report.rebuilt
            and report.missing == (lost.source_id,)
            and report.extra == (ghost.source_id,)
        )
        assert ids(await world.ask(A, "маяк")) == {keep.source_id, lost.source_id}
        assert (await world.memory.reconcile(A)).rebuilt is False
        await world.store.close()

    run(scenario())


def test_hostile_queries_stay_data(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, "Обычная запись про маяк.", external_id="q-1")
        for query in (
            'маяк" OR "x',
            "маяк NEAR(a b) AND NOT c",
            "*",
            "маяк\x00",
            "'; DROP TABLE sources;--",
        ):
            result = await world.ask(A, query)
            assert result.index_state in {"ready", "degraded"}
        assert made.source_id in ids(await world.ask(A, 'маяк" OR "x'))
        assert (await world.index.inventory(A)) == {made.source_id: 1}
        await world.store.close()

    run(scenario())


def test_index_file_is_separate_and_never_the_main_database(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.add(A, "Запись про маяк.", external_id="s-1")
        import sqlite3

        databases = sorted(tmp_path.glob("*.sqlite"))
        assert len(databases) == 2
        tables = {}
        for path in databases:
            with sqlite3.connect(path) as connection:
                tables[path.name] = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        main = next(v for v in tables.values() if "record_revisions" in v)
        derived = next(v for v in tables.values() if "chunks" in v)
        assert not (main & {"chunks", "lexical", "claim_sources"})
        assert not (derived & {"records", "record_revisions", "processed_operations"})
        await world.store.close()

    run(scenario())


def test_conflict_ids_follow_the_readers_access_to_the_claims(tmp_path):
    async def scenario():
        acl = (RecordAccess(OPERATOR.id, read=True),)  # sources are shared; claims are not
        world = await new_world(tmp_path, [A], reader_access=acl)
        first = await world.add(A, "Один сказал про мост.", external_id="k-1")
        second = await world.add(A, "Другой сказал иное про мост.", external_id="k-2")
        s1 = await world.memory.get_source(A, first.source_id)
        s2 = await world.memory.get_source(A, second.source_id)
        await world.memory.record_claim(
            command(A),
            ClaimDraft("claim-1", "subject-1", "Версия один", "reported", (ref(s1),), 0.4),
        )
        await world.memory.record_claim(
            command(A),
            ClaimDraft(
                "claim-2", "subject-1", "Версия два", "reported", (ref(s2),), 0.4, ("claim-1",)
            ),
        )
        owner = {f.source.source_id: f.conflicts for f in (await world.ask(A, "мост")).fragments}
        assert owner == {first.source_id: ["claim-1"], second.source_id: ["claim-2"]}
        reader = RecallService(
            world.store, OPERATOR, world.index, embedder=world.embedder, clock=world.clock
        )
        seen = await world.ask(A, "мост", service=reader)
        assert ids(seen) == {first.source_id, second.source_id}
        assert all(f.conflicts == [] for f in seen.fragments)  # claim IDs are not leaked
        await world.store.close()

    run(scenario())
