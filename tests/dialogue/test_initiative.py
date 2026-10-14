"""Initiative from exact obligations, contact preferences and context fairness (M2-06)."""

from datetime import timedelta

from ai_agent_application.dialogue import ContextPolicy
from ai_agent_contracts.states import ActivityState, DeliveryState
from dialogue_support import decision, eid, new_world, run, script, viewer

REMINDER = script(decision("Напоминаю про находку — готова показать."))


async def promised(world, controller, key, *, days=0, minutes=1, seconds=0, person=None):
    """A person exists and the character has *delivered* a promise to them."""
    if person is None:
        controller.submit(viewer("Привет!", event=eid("hello")))
        first = await controller.step()
        person = first.person_id
        await controller.settle(
            first.draft.utterance_id,
            DeliveryState.DELIVERED,
            await world.deliver(first.draft.utterance_id),
        )
    uid = f"utt-{key}"
    delivery = await world.deliver(uid)
    source = await world.memory_source(f"Обещание {key}", scope=controller.scope)
    await world.social.record_promise(
        world.command(controller.scope),
        [person],
        key,
        delivery_id=delivery,
        utterance_id=uid,
        promise_key=key,
        content=f"Показать {key}",
        completion_criterion="Показано",
        sources=[source],
        deadline=world.clock.now + timedelta(days=days, minutes=minutes, seconds=seconds),
    )
    return person


def hello():
    return script(decision("Привет!"))


def test_a_due_promise_becomes_an_initiative_from_the_exact_record(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), REMINDER])
        controller = world.controller()
        person = await promised(world, controller, "find")
        early = (
            await controller.collect_initiatives()
        )  # not due yet within the lookahead? 1 min < 5
        world.clock.advance(minutes=10)
        offered = await controller.collect_initiatives()
        outcome = await controller.step()
        settled = await controller.settle(
            outcome.draft.utterance_id,
            DeliveryState.DELIVERED,
            await world.deliver(outcome.draft.utterance_id),
        )
        return world, person, early, offered, outcome, settled

    world, person, early, offered, outcome, settled = run(scenario())
    assert len(early) == 1 and len(offered) == 0  # offered once already; not duplicated
    assert outcome.status == "drafted" and outcome.draft.initiative and outcome.person_id == person
    assert "<obligation" in world.user_prompt() and "Показать find" in world.user_prompt()
    assert not settled.pending


def test_reminders_respect_gap_interval_and_the_cap(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello()] + [REMINDER] * 6)
        controller = world.controller()
        await promised(world, controller, "find")
        counts = []
        for _ in range(5):
            world.clock.advance(hours=1, minutes=1)
            offered = await controller.collect_initiatives()
            counts.append(len(offered))
            outcome = await controller.step()
            if outcome is not None:
                uid = outcome.draft.utterance_id
                await controller.settle(uid, DeliveryState.DELIVERED, await world.deliver(uid))
                world.clock.advance(minutes=30)
                controller.limiter.answered(
                    outcome.person_id
                )  # keep the person-level limits out of this test
        return counts

    assert run(scenario()) == [1, 1, 1, 0, 0]  # max_reminders = 3 per promise


def test_two_unanswered_initiatives_mean_ten_minutes_of_silence_until_the_person_writes(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), REMINDER, REMINDER, hello()])
        controller = world.controller()
        person = await promised(world, controller, "one")
        await promised(world, controller, "two", person=person)
        await promised(world, controller, "three", person=person)
        world.clock.advance(hours=2)
        offered = await controller.collect_initiatives()
        outcomes = []
        for _ in range(3):
            out = await controller.step()
            outcomes.append(out)
            if out.status == "drafted":
                uid = out.draft.utterance_id
                await controller.settle(uid, DeliveryState.DELIVERED, await world.deliver(uid))
            world.clock.advance(seconds=40)
        calls = len(world.provider.calls)
        # the person answers: the silence is over and a new reminder may be sent
        controller.submit(viewer("Я тут!", event="back"))
        answered = await controller.step()
        now = world.clock.now
        return (
            offered,
            outcomes,
            calls,
            answered,
            controller.limiter.allowed(person, now),
            (controller.limiter.allowed(person, now + timedelta(minutes=6))),
        )

    offered, outcomes, calls, answered, allowed, later = run(scenario())
    assert len(offered) == 3
    assert [o.status for o in outcomes] == ["drafted", "drafted", "held"]
    assert outcomes[2].reason == "SILENCE_AFTER_UNANSWERED" and calls == 3  # hello + two reminders
    assert answered.status == "drafted"
    # the person's message ended the silence, but at most two per five minutes still holds
    assert allowed == (False, "WINDOW_LIMIT") and later == (True, None)


def test_do_not_contact_outranks_a_due_promise_and_observation_mode_has_no_initiative(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), REMINDER])
        controller = world.controller()
        person = await promised(world, controller, "find")
        source = await world.memory_source(
            "Пожалуйста, не пиши мне первой.", scope=controller.scope
        )
        await world.social.ensure_relationship(
            world.command(controller.scope), person, {"trust": 0.5}
        )
        await world.social.set_contact_preference(
            world.command(controller.scope), person, 1, "no_contact", source
        )
        world.clock.advance(hours=2)
        blocked = await controller.collect_initiatives()
        return world, controller, blocked

    world, controller, blocked = run(scenario())
    assert blocked == [] and controller.stats["initiative_blocked_contact"] == 1
    world.control.autonomy = "observation"
    assert run(controller.collect_initiatives()) == []


def test_no_initiative_while_paused_and_a_queued_reminder_is_rechecked_before_it_is_sent(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), REMINDER])
        controller = world.controller()
        person = await promised(world, controller, "find")
        world.clock.advance(hours=2)
        world.control.activity = ActivityState.PAUSED
        paused = await controller.collect_initiatives()
        world.control.activity = ActivityState.OPEN
        offered = await controller.collect_initiatives()
        # the person asks to be left alone while the reminder waits in the queue
        source = await world.memory_source("Не пиши мне.", scope=controller.scope)
        await world.social.ensure_relationship(
            world.command(controller.scope), person, {"trust": 0.5}
        )
        await world.social.set_contact_preference(
            world.command(controller.scope), person, 1, "no_contact", source
        )
        outcome = await controller.step()
        return paused, offered, outcome, len(world.provider.calls)

    paused, offered, outcome, calls = run(scenario())
    assert paused == [] and len(offered) == 1
    assert outcome.status == "held" and outcome.reason == "DO_NOT_CONTACT" and calls == 1


def test_all_due_obligations_are_found_without_a_search_and_context_cuts_are_reported(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), script(decision("Помню."))])
        controller = world.controller()
        person = await promised(world, controller, "k0", days=1)
        for i in range(1, 5):
            await promised(world, controller, f"k{i}", days=i + 1, person=person)
        tight = world.controller("session-2")
        tight.context.policy = ContextPolicy(max_obligations=3)
        tight.submit(viewer("Что ты мне обещала?", event="ask"))
        outcome = await tight.step()
        every = await world.social.obligations(world.base)
        return world, outcome, every

    world, outcome, every = run(scenario())
    assert len(every) == 5  # the exact scan is not capped by the context policy
    prompt = world.user_prompt()
    assert (
        prompt.count("<obligation") == 3 and "Показать k0" in prompt and "Показать k4" not in prompt
    )
    sections = {s.name: s for s in outcome.manifest.sections}
    assert sections["obligations"].included == 3 and sections["obligations"].omitted == 2


def test_a_second_reminder_inside_the_minimum_gap_is_not_even_queued(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [hello(), REMINDER])
        controller = world.controller()
        person = await promised(world, controller, "one")
        world.clock.advance(hours=2)
        # becomes due (inside the 5-minute lookahead) only 10 s after the first reminder
        await promised(world, controller, "two", person=person, minutes=5, seconds=5)
        first = await controller.collect_initiatives()
        out = await controller.step()
        uid = out.draft.utterance_id
        await controller.settle(uid, DeliveryState.DELIVERED, await world.deliver(uid))
        world.clock.advance(seconds=10)
        second = await controller.collect_initiatives()
        world.clock.advance(seconds=30)
        third = await controller.collect_initiatives()
        return len(first), second, len(third), controller.stats

    first, second, third, stats = run(scenario())
    assert first == 1 and second == [] and stats["initiative_blocked_MIN_GAP"] == 1
    assert third == 1  # the gap passed; the reminder is queued
