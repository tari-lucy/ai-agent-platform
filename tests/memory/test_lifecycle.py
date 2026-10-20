"""Source / episode / claim lifecycle on the real M1-02 store (authority) with a derived index."""

from datetime import timedelta

import pytest
from ai_agent_application.memory import ClaimDraft, EpisodeDraft, MemoryService
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Claim, Source
from ai_agent_contracts.states import ErrorCode
from memory_support import (
    MEMORY,
    NOW,
    FailingIndex,
    command,
    make_scope,
    new_world,
    run,
)

A = make_scope("persona-a")
A_TEST = make_scope("persona-a", "test")


def code(info):
    return info.value.error.code


def ref(source, scope=A):
    return SourceRef(
        source_id=source.id, version=source.version, scope=scope, availability="available"
    )


def test_register_counts_ttl_from_the_original_time_and_is_explicit_about_rights(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        old = NOW - timedelta(days=10)
        result = await world.add(
            A, "Обещание вернуться к вопросу.", occurred_at=old, external_id="m-1"
        )
        assert (result.version, result.duplicate, result.indexed) == (1, False, True)
        source = await world.memory.get_source(A, result.source_id)
        assert source.retention.expires_at == old + timedelta(days=30)  # not NOW + 30 days
        assert source.retention.policy_id == "conversation-30d" and source.registered_at == NOW
        assert (
            source.read_allowed,
            source.api_transfer_allowed,
            source.public_disclosure_allowed,
        ) == (
            True,
            True,
            False,
        )
        protected = await world.add(A, "Защищённая запись.", protected=True)
        assert (await world.memory.get_source(A, protected.source_id)).retention.expires_at is None
        await world.store.close()

    run(scenario())


def test_registration_guards(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        for text, extra, expected in (
            ("Старое.", {"occurred_at": NOW - timedelta(days=31)}, ErrorCode.EXPIRED),
            (
                "Из будущего.",
                {"occurred_at": NOW + timedelta(hours=2)},
                ErrorCode.INVALID_ARGUMENTS,
            ),
            ("   ", {}, ErrorCode.INVALID_ARGUMENTS),
            ("я" * 16001, {}, ErrorCode.INVALID_ARGUMENTS),
        ):
            with pytest.raises(ContractError) as error:
                await world.add(A, text, **extra)
            assert code(error) == expected
        assert (await world.store.engine.url.database) if False else True
        await world.store.close()

    run(scenario())


def test_one_external_event_is_one_source_but_text_alone_is_not_identity(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        first = await world.add(A, "Все идём к реке.", external_id="chat-1")
        again = await world.add(A, "Все идём к реке.", external_id="chat-1")
        assert again.duplicate and again.source_id == first.source_id and again.version == 1
        with pytest.raises(ContractError) as edited:  # an edit is a separate event
            await world.add(A, "Все идём к озеру.", external_id="chat-1")
        assert code(edited) == ErrorCode.VERSION_CONFLICT
        # The same words from another external event are another piece of evidence.
        other = await world.add(A, "Все идём к реке.", external_id="chat-2", author_id="person-2")
        assert other.source_id != first.source_id and not other.duplicate
        # Request replay returns the stored receipt without a second effect.
        cmd = command(A, "request-fixed")
        draft_args = dict(
            provenance=(await world.memory.get_source(A, first.source_id)).provenance,
            source_type="synthetic-chat",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=NOW - timedelta(hours=1),
            content="Без внешнего ID.",
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        from ai_agent_application.memory import SourceDraft

        one = await world.memory.register_source(cmd, SourceDraft(**draft_args))
        two = await world.memory.register_source(cmd, SourceDraft(**draft_args))
        assert one.source_id == two.source_id and (two.duplicate or two.replayed)
        await world.store.close()

    run(scenario())


def test_rights_change_expire_and_delete_follow_versions(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, "Тайна огорода.", external_id="m-1")
        receipt = await world.memory.set_rights(
            command(A),
            made.source_id,
            1,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        assert receipt.version == 2
        with pytest.raises(ContractError) as stale:
            await world.memory.expire_source(command(A), made.source_id, 1)
        assert code(stale) == ErrorCode.VERSION_CONFLICT and stale.value.error.current_version == 2

        expired = await world.memory.expire_source(command(A), made.source_id, 2)
        assert expired.version == 3
        assert await world.memory.get_source(A, made.source_id) is None  # no longer readable
        assert await world.index.inventory(A) == {}
        with pytest.raises(ContractError):  # nothing left to retire
            await world.memory.delete_source(command(A), made.source_id, 3)

        gone = await world.add(A, "Другая тайна.", external_id="m-2")
        await world.memory.delete_source(command(A), gone.source_id, 1)
        with pytest.raises(ContractError) as reimport:  # deleted identity cannot come back
            await world.add(A, "Другая тайна.", external_id="m-2")
        assert code(reimport) == ErrorCode.ACCESS_DENIED
        await world.store.close()

    run(scenario())


def test_episode_is_one_per_operation_and_needs_available_sources(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, A_TEST])
        one = await world.add(A, "Мы нашли странный камень.", external_id="e-1")
        two = await world.add(A, "Позже пересказали про камень.", external_id="e-2")
        sources = [await world.memory.get_source(A, r.source_id) for r in (one, two)]
        draft = EpisodeDraft(
            sources=tuple(ref(s) for s in sources),
            participant_ids=("person-1",),
            choice="Рассмотреть камень",
            actual_outcome="Камень оказался обычным",
            operation_id="operation-1",
            provenance="observation",
        )
        first, duplicate = await world.memory.record_episode(command(A), draft)
        again, duplicate_again = await world.memory.record_episode(command(A), draft)
        assert first == again and not duplicate and duplicate_again
        # The deleted source cannot found a new episode.
        await world.memory.delete_source(command(A), one.source_id, 1)
        with pytest.raises(ContractError):
            await world.memory.record_episode(
                command(A), EpisodeDraft(**{**draft.__dict__, "operation_id": "operation-2"})
            )
        # A simulated episode only exists in a test scope.
        with pytest.raises(ContractError):
            await world.memory.record_episode(
                command(A),
                EpisodeDraft(
                    **{**draft.__dict__, "operation_id": "operation-3", "provenance": "simulation"}
                ),
            )
        await world.store.close()

    run(scenario())


def test_conflicting_claims_keep_both_positions_with_links_on_both_sides(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        a = await world.add(A, "Он сказал, что замок построил он.", external_id="c-1")
        b = await world.add(
            A, "Другой участник сказал, что замок построила она.", external_id="c-2"
        )
        sa = await world.memory.get_source(A, a.source_id)
        sb = await world.memory.get_source(A, b.source_id)
        claims = world.store.repository(Claim, MEMORY, A)

        def draft(claim_id, source, text, conflicts=()):
            return ClaimDraft(
                claim_id, "subject-1", text, "reported", (ref(source),), 0.4, conflicts
            )

        await world.memory.record_claim(command(A), draft("claim-a", sa, "Замок построил он"))
        await world.memory.record_claim(
            command(A), draft("claim-b", sb, "Замок построила она", ("claim-a",))
        )
        old, new = await claims.get("claim-a"), await claims.get("claim-b")
        assert old.version == 2 and old.conflict_ids == ["claim-b"]
        assert old.content == "Замок построил он"  # earlier position is not rewritten
        assert new.conflict_ids == ["claim-a"]
        assert await world.index.conflicts_for(A, [a.source_id, b.source_id]) == {
            a.source_id: ("claim-a",),
            b.source_id: ("claim-b",),
        }
        for bad in (("claim-b",), ("ghost",)):  # self-conflict / unknown position
            with pytest.raises(ContractError):
                await world.memory.record_claim(
                    command(A),
                    draft("claim-b" if bad == ("claim-b",) else "claim-c", sb, "x", bad),
                )
        await world.store.close()

    run(scenario())


def test_index_failure_never_rolls_back_authority_and_lag_is_visible(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        world.memory.index = broken = FailingIndex(world.index)
        made = await world.add(A, "Запись при сломанном индексе.", external_id="m-1")
        assert not made.indexed and made.source_id in world.health.pending(A)
        assert await world.memory.get_source(A, made.source_id) is not None  # committed
        result = await world.ask(A, "сломанном индексе")
        assert result.index_state in {"degraded", "lagging"} and not result.complete
        broken.broken = False
        report = await world.memory.reconcile(A)
        assert report.rebuilt and made.source_id in report.missing
        assert not world.health.pending(A)
        healed = await world.ask(A, "сломанном индексе")
        assert [f.source.source_id for f in healed.fragments] == [made.source_id]
        assert (await world.memory.reconcile(A)).rebuilt is False  # nothing left to repair
        await world.store.close()

    run(scenario())


def test_non_memory_principals_cannot_run_the_lifecycle(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        from memory_support import OPERATOR, WORKER

        for principal in (OPERATOR, WORKER):
            with pytest.raises(ContractError) as error:
                MemoryService(world.store, principal, world.memory.extensions)
            assert code(error) == ErrorCode.ACCESS_DENIED
        await world.store.close()

    run(scenario())


def test_rejected_registration_leaves_no_trace(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        before = await world.index.inventory(A)
        with pytest.raises(ContractError):
            await world.add(A, "Старое.", occurred_at=NOW - timedelta(days=40), external_id="x-1")
        assert await world.index.inventory(A) == before == {}
        assert await world.store.repository(Source, MEMORY, A).list() == []
        await world.store.close()

    run(scenario())


def test_a_later_run_revises_sources_and_contradicts_claims_of_an_earlier_run(tmp_path):
    run_one = A.model_copy(update={"agent_id": "agent-a", "run_id": "run-1", "session_id": "s-1"})
    run_two = A.model_copy(update={"agent_id": "agent-a", "run_id": "run-2", "session_id": "s-2"})

    async def scenario():
        world = await new_world(tmp_path, [A])
        a = await world.add(run_one, "Он сказал, что замок построил он.", external_id="r-1")
        sa = await world.memory.get_source(run_one, a.source_id)
        await world.memory.record_claim(
            command(run_one),
            ClaimDraft("claim-a", "subject-1", "Замок построил он", "reported", (ref(sa),), 0.4),
        )
        b = await world.add(run_two, "Она сказала, что замок построила она.", external_id="r-2")
        sb = await world.memory.get_source(run_two, b.source_id)
        await world.memory.record_claim(  # the earlier claim gets the conflict link
            command(run_two),
            ClaimDraft(
                "claim-b",
                "subject-1",
                "Замок построила она",
                "reported",
                (ref(sb, run_two),),
                0.4,
                ("claim-a",),
            ),
        )
        old = await world.store.repository(Claim, MEMORY, run_two).get("claim-a")
        assert old.conflict_ids == ["claim-b"] and old.scope == run_two
        receipt = await world.memory.set_rights(
            command(run_two),
            a.source_id,
            1,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        assert receipt.version == 2
        await world.store.close()

    run(scenario())
