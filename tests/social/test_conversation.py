"""Question reservations, known answers and exact promises (M2-03)."""

import asyncio
from datetime import timedelta

import pytest
from ai_agent_adapters.storage.unit_of_work import SQLiteUnitOfWork
from ai_agent_application.social import SocialPolicy
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Promise
from ai_agent_contracts.states import ErrorCode
from social_support import command, make_scope, new_world, run

A = make_scope("persona-a")
TOPIC = "Любимый цвет?"


def code(info):
    return info.value.error.code


async def asked(world, scope, person, key=TOPIC, operation="op-1"):
    decision = await world.social.reserve_question(command(scope), person.id, key, operation)
    assert decision.allowed
    return decision


def test_a_question_is_reserved_once_even_when_tasks_race(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, _ = await world.person(A)
        decisions = await asyncio.gather(
            *(
                world.social.reserve_question(command(A), person.id, TOPIC, f"op-{n}")
                for n in range(6)
            )
        )
        winners = [d for d in decisions if d.allowed]
        assert len(winners) == 1
        assert {d.reason for d in decisions if not d.allowed} == {"RESERVED"}
        # Rewording / spacing / case do not make a new topic.
        reworded = await world.social.reserve_question(
            command(A), person.id, "  любимый   ЦВЕТ ", "op-late"
        )
        assert not reworded.allowed and reworded.topic_id == winners[0].topic_id
        await world.store.close()

    run(scenario())


def test_blocked_requests_write_nothing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, _ = await world.person(A)
        await asked(world, A, person)
        before = await world.social.get_topic(A, person.id, TOPIC)
        blocked = await world.social.reserve_question(command(A), person.id, TOPIC, "op-2")
        assert not blocked.allowed and blocked.retry_after is not None
        assert await world.social.get_topic(A, person.id, TOPIC) == before
        await world.store.close()

    run(scenario())


def test_delivered_question_waits_for_the_answer_then_the_answer_is_known(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        await asked(world, A, person)
        topic = await world.social.settle_question(
            command(A), person.id, TOPIC, "op-1", "delivered", "utterance-1"
        )
        assert topic.status == "awaiting_answer" and topic.utterance_ids == ["utterance-1"]
        # Voice and chat of the same utterance settle once.
        again = await world.social.settle_question(
            command(A), person.id, TOPIC, "op-1", "delivered", "utterance-1"
        )
        assert again.version == topic.version
        for _ in range(2):
            pending = await world.social.reserve_question(command(A), person.id, TOPIC, "op-9")
            assert (pending.allowed, pending.reason) == (False, "AWAITING_ANSWER")

        reply = await world.source(A, "Мой любимый цвет — зелёный.")
        answered = await world.social.record_answer(
            command(A), person.id, TOPIC, "зелёный", [reply]
        )
        known = await world.social.reserve_question(command(A), person.id, TOPIC, "op-10")
        assert (known.allowed, known.reason, known.answer) == (False, "ANSWER_KNOWN", "зелёный")
        with pytest.raises(
            ContractError
        ) as overwrite:  # a correction names the version it replaces
            await world.social.record_answer(command(A), person.id, TOPIC, "синий", [reply])
        assert code(overwrite) == ErrorCode.VERSION_CONFLICT
        fixed = await world.social.record_answer(
            command(A), person.id, TOPIC, "синий", [reply], expected_version=answered.version
        )
        assert fixed.answer == "синий" and fixed.version == answered.version + 1
        with pytest.raises(ContractError):  # an answer without a source is not knowledge
            await world.social.record_answer(command(A), person.id, "возраст", "30", [])
        await world.store.close()

    run(scenario())


def test_a_blocked_draft_is_not_a_question_and_an_unknown_delivery_is_held(tmp_path):
    async def scenario():
        policy = SocialPolicy(
            reservation_ttl=timedelta(minutes=2), unknown_delivery_hold=timedelta(minutes=30)
        )
        world = await new_world(tmp_path, [A], policy=policy)
        person, _ = await world.person(A)
        await asked(world, A, person)
        released = await world.social.settle_question(
            command(A), person.id, TOPIC, "op-1", "not_delivered"
        )
        assert released.status == "open" and released.utterance_ids == []
        assert (await world.social.reserve_question(command(A), person.id, TOPIC, "op-2")).allowed

        await world.social.settle_question(command(A), person.id, TOPIC, "op-2", "unknown")
        world.clock.advance(minutes=10)  # past the normal reservation, inside the hold
        held = await world.social.reserve_question(command(A), person.id, TOPIC, "op-3")
        assert (held.allowed, held.reason) == (False, "RESERVED")
        # Reconciliation later learns the truth and settles with the same operation.
        world.clock.advance(minutes=60)
        settled = await world.social.settle_question(
            command(A), person.id, TOPIC, "op-2", "delivered", "utterance-7"
        )
        assert settled.status == "awaiting_answer"
        with pytest.raises(ContractError) as foreign:  # not the reservation holder
            await world.social.settle_question(
                command(A), person.id, TOPIC, "op-x", "delivered", "u-8"
            )
        assert code(foreign) == ErrorCode.STALE_CONTEXT
        await world.store.close()

    run(scenario())


def test_reservations_expire_and_cooldown_blocks_until_it_ends(tmp_path):
    async def scenario():
        policy = SocialPolicy(
            reservation_ttl=timedelta(minutes=2), cooldown_after_question=timedelta(minutes=10)
        )
        world = await new_world(tmp_path, [A], policy=policy)
        person, _ = await world.person(A)
        await asked(world, A, person, key="цвет", operation="op-1")
        world.clock.advance(minutes=3)  # holder vanished before settling
        assert (await world.social.reserve_question(command(A), person.id, "цвет", "op-2")).allowed

        await world.social.settle_question(
            command(A), person.id, "цвет", "op-2", "delivered", "u-1"
        )
        topic = await world.social.get_topic(A, person.id, "цвет")
        # Unanswered but re-askable only after an explicit decision: close it, then it stays closed.
        closed = await world.social.close_topic(command(A), person.id, "цвет", topic.version)
        blocked = await world.social.reserve_question(command(A), person.id, "цвет", "op-3")
        assert closed.status == "closed" and blocked.reason == "CLOSED"

        await asked(world, A, person, key="имя", operation="op-4")
        await world.social.settle_question(command(A), person.id, "имя", "op-4", "not_delivered")
        assert (await world.social.reserve_question(command(A), person.id, "имя", "op-5")).allowed
        await world.store.close()

    run(scenario())


def test_cooldown_after_a_delivered_question_survives_an_explicit_reopen(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, _ = await world.person(A)
        await asked(world, A, person, key="хобби", operation="op-1")
        delivered = await world.social.settle_question(
            command(A), person.id, "хобби", "op-1", "delivered", "u-1"
        )
        ends = world.clock.now + world.social.policy.cooldown_after_question
        assert delivered.cooldown_until == ends
        reopened = await world.social.reopen_topic(
            command(A), person.id, "хобби", delivered.version
        )
        assert reopened.status == "open"
        early = await world.social.reserve_question(command(A), person.id, "хобби", "op-2")
        assert (early.allowed, early.reason, early.retry_after) == (False, "COOLDOWN", ends)
        world.clock.advance(minutes=11)
        assert (await world.social.reserve_question(command(A), person.id, "хобби", "op-2")).allowed
        with pytest.raises(ContractError):  # only an awaiting or closed topic can be reopened
            await world.social.reopen_topic(command(A), person.id, "хобби", 3)
        await world.store.close()

    run(scenario())


def test_a_stated_wish_not_to_be_contacted_outranks_everything(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        await world.social.ensure_relationship(command(A), person.id, {"trust": 0.5})
        assert (await world.social.reserve_question(command(A), person.id, "цвет", "op-1")).allowed
        await world.social.set_contact_preference(command(A), person.id, 1, "no_contact", evidence)
        for key in ("цвет", "другая тема"):
            blocked = await world.social.reserve_question(command(A), person.id, key, "op-2")
            assert (blocked.allowed, blocked.reason) == (False, "DO_NOT_CONTACT")
        context = await world.social.context_for(A, person.id)
        assert context.contact == "no_contact"
        await world.store.close()

    run(scenario())


def test_a_promise_exists_only_after_a_delivered_utterance(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        social = world.social
        kwargs = dict(
            promise_key="сравнить идеи",
            content="Помочь сравнить две идеи",
            completion_criterion="Сравнение отправлено",
            sources=[evidence],
        )
        # Queued, failed or unknown deliveries, a missing record, another utterance: no promise.
        await world.delivery(A, "delivery-q", "utt-1", "QUEUED")
        await world.delivery(A, "delivery-f", "utt-1", "FAILED")
        await world.delivery(A, "delivery-u", "utt-1", "UNKNOWN")
        await world.delivery(A, "delivery-ok", "utt-1", "DELIVERED")
        for delivery_id, utterance in (
            ("delivery-none", "utt-1"),
            ("delivery-q", "utt-1"),
            ("delivery-f", "utt-1"),
            ("delivery-u", "utt-1"),
            ("delivery-ok", "utt-OTHER"),
        ):
            with pytest.raises(ContractError):
                await social.record_promise(
                    command(A),
                    [person.id],
                    "идеи",
                    delivery_id=delivery_id,
                    utterance_id=utterance,
                    **kwargs,
                )
        assert await social.obligations(A) == []

        promise, duplicate = await social.record_promise(
            command(A),
            [person.id],
            "идеи",
            delivery_id="delivery-ok",
            utterance_id="utt-1",
            **kwargs,
        )
        assert not duplicate and promise.status == "open" and promise.delivery_id == "delivery-ok"
        # The voice delivery of the very same utterance is the same promise, not a second one.
        await world.delivery(A, "delivery-voice", "utt-1", "DELIVERED")
        same, duplicate = await social.record_promise(
            command(A),
            [person.id],
            "идеи",
            delivery_id="delivery-voice",
            utterance_id="utt-1",
            **kwargs,
        )
        assert duplicate and same.id == promise.id
        assert len(await social.obligations(A, person.id)) == 1
        await world.store.close()

    run(scenario())


def test_closing_a_promise_needs_its_own_evidence_and_is_final(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        await world.delivery(A, "delivery-ok", "utt-1")
        social = world.social

        async def make(key):
            made, _ = await social.record_promise(
                command(A),
                [person.id],
                "идеи",
                delivery_id="delivery-ok",
                utterance_id="utt-1",
                promise_key=key,
                content="Сделать",
                completion_criterion="Готово",
                sources=[evidence],
            )
            return made

        kept = await make("a")
        for bad in (
            dict(outcome="fulfilled", closure_result="сделано"),  # no evidence
            dict(outcome="fulfilled", closure_result=None, evidence=evidence),
            dict(outcome="withdrawn", closure_result=None),
        ):
            with pytest.raises(ContractError):
                await social.close_promise(command(A), kept.id, 1, **bad)
        proof = await world.source(A, "Спасибо, сравнение получил.")
        done = await social.close_promise(
            command(A), kept.id, 1, "fulfilled", "Сравнение отправлено", proof
        )
        assert done.status == "fulfilled" and proof.source_id in {s.source_id for s in done.sources}
        with pytest.raises(ContractError):  # terminal
            await social.close_promise(command(A), kept.id, 2, "withdrawn", "передумал")
        assert await social.obligations(A) == []

        unsure = await make("b")
        marked = await social.close_promise(command(A), unsure.id, 1, "unknown", None)
        assert marked.status == "unknown"
        assert [o.promise.id for o in await social.obligations(A)] == [unsure.id]  # still owed
        await world.store.close()

    run(scenario())


def test_exact_obligations_are_complete_beyond_any_top_k_or_page(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A, "acct-1")
        other, _ = await world.person(A, "acct-2", name="Миша")
        await world.delivery(A, "delivery-ok", "utt-1")
        social = world.social
        total = 230  # more than one 200-record page
        for number in range(total):
            await social.record_promise(
                command(A),
                [person.id if number % 2 == 0 else other.id],
                "идеи",
                delivery_id="delivery-ok",
                utterance_id="utt-1",
                promise_key=f"обещание {number}",
                content=f"Сделать №{number}",
                completion_criterion="Готово",
                sources=[evidence],
                deadline=world.clock.now + timedelta(days=number + 1),
            )
        mine = await social.obligations(A, person.id)
        assert len(mine) == total // 2 and all(person.id in o.promise.participant_ids for o in mine)
        assert len(await social.obligations(A)) == total
        assert [o.promise.deadline for o in mine] == sorted(o.promise.deadline for o in mine)
        await world.store.close()

    run(scenario())


def test_an_expired_conversation_does_not_erase_or_fulfil_a_promise(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        await world.delivery(A, "delivery-ok", "utt-1")
        promise, _ = await world.social.record_promise(
            command(A),
            [person.id],
            "идеи",
            delivery_id="delivery-ok",
            utterance_id="utt-1",
            promise_key="x",
            content="Вернуться к теме",
            completion_criterion="Ответ дан",
            sources=[evidence],
            deadline=world.clock.now + timedelta(days=5),
        )
        world.clock.advance(days=40)  # raw conversation text is past its 30 days
        kept = await world.social.obligations(A, person.id)
        assert [o.promise.id for o in kept] == [promise.id]
        assert kept[0].promise.status == "open" and kept[0].overdue
        assert kept[0].promise.sources[0].availability == "expired"
        context = await world.social.context_for(A, person.id)
        assert context is not None and len(context.obligations) == 1
        await world.store.close()

    run(scenario())


def test_a_failed_save_never_pretends_the_promise_was_remembered(tmp_path, monkeypatch):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        await world.delivery(A, "delivery-ok", "utt-1")
        original = SQLiteUnitOfWork.put

        async def failing(self, record, **kwargs):
            if isinstance(record, Promise):
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            return await original(self, record, **kwargs)

        monkeypatch.setattr(SQLiteUnitOfWork, "put", failing)
        with pytest.raises(ContractError):
            await world.social.record_promise(
                command(A),
                [person.id],
                "идеи",
                delivery_id="delivery-ok",
                utterance_id="utt-1",
                promise_key="x",
                content="Вернуться",
                completion_criterion="Ответ",
                sources=[evidence],
            )
        monkeypatch.setattr(SQLiteUnitOfWork, "put", original)
        assert await world.social.obligations(A) == []
        await world.store.close()

    run(scenario())


def test_context_collects_everything_exact_for_a_returning_person(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        person, evidence = await world.person(A)
        social = world.social
        await social.ensure_relationship(command(A), person.id, {"trust": 0.5})
        await social.record_encounter(command(A), [person.id], [evidence], "session-1")
        await asked(world, A, person, key="цвет", operation="op-1")
        await social.settle_question(command(A), person.id, "цвет", "op-1", "delivered", "u-1")
        reply = await world.source(A, "Зелёный.")
        await social.record_answer(command(A), person.id, "цвет", "зелёный", [reply])
        await asked(world, A, person, key="город", operation="op-2")
        await social.settle_question(command(A), person.id, "город", "op-2", "delivered", "u-2")
        await world.delivery(A, "delivery-ok", "utt-1")
        await social.record_promise(
            command(A),
            [person.id],
            "идеи",
            delivery_id="delivery-ok",
            utterance_id="utt-1",
            promise_key="p",
            content="Вернуться",
            completion_criterion="Ответ",
            sources=[evidence],
        )
        restarted = world.restarted()
        context = await restarted.context_for(A, person.id)
        assert context.returning and context.meetings == 1 and context.contact == "normal"
        assert [t.answer for t in context.known_answers] == ["зелёный"]
        assert [t.status for t in context.open_topics] == ["awaiting_answer"]
        assert len(context.obligations) == 1 and context.relationship is not None
        assert await restarted.context_for(A, "person-unknown") is None
        await world.store.close()

    run(scenario())


def test_a_promise_made_in_one_run_is_closed_in_a_later_run_of_the_same_partition(tmp_path):
    run_one = A.model_copy(update={"agent_id": "agent-a", "run_id": "run-1", "session_id": "s-1"})
    run_two = A.model_copy(update={"agent_id": "agent-a", "run_id": "run-2", "session_id": "s-2"})
    other = make_scope("persona-b")

    async def scenario():
        world = await new_world(tmp_path, [A, other])
        person, evidence = await world.person(run_one)
        await world.delivery(run_one, "delivery-ok", "utt-1")
        made, _ = await world.social.record_promise(
            command(run_one),
            [person.id],
            "идеи",
            delivery_id="delivery-ok",
            utterance_id="utt-1",
            promise_key="a",
            content="Сделать",
            completion_criterion="Готово",
            sources=[evidence],
        )
        assert made.scope == run_one
        with pytest.raises(ContractError):  # another persona's partition never sees it
            await world.social.close_promise(command(other), made.id, 1, "withdrawn", "нет")
        closed = await world.social.close_promise(
            command(run_two), made.id, 1, "withdrawn", "Оператор передумал."
        )
        assert closed.status == "withdrawn"
        stored = await world.social._repo(Promise, run_two).get(made.id)
        assert stored is not None and stored.scope == run_two and stored.version == 2
        first = await world.social._repo(Promise, run_two).get(made.id, version=1)
        assert first is not None and first.status == "open"  # history is kept
        await world.store.close()

    run(scenario())
