"""Internal game master on real SQLite (M3-02): limits, screen, lifecycle, no forced outcomes."""

from datetime import timedelta

import pytest
from ai_agent_application.gamemaster import Classification, Dice, StoryRule, StoryRules, screen
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Episode
from ai_agent_contracts.persona import Opportunity, StoryArc
from ai_agent_contracts.states import ErrorCode, Provenance
from ai_agent_contracts.states import OpportunityState as S
from gm_support import (
    FAMILIES,
    MEMORY,
    NOW,
    RULES,
    draft,
    make_scope,
    new_world,
    run,
)


def test_an_arc_needs_first_hand_grounds_and_is_limited(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        own = await world.source(
            "Я сказала про реку.",
            source_type="character-utterance",
            provenance=Provenance.OBSERVATION,
        )
        recap = await world.source("Сводка про реку.", provenance=Provenance.MODEL_PROPOSAL)
        bad = [await world.gm.open_arc(world.scope, "k1", [g], "d", "b") for g in (own, recap)]
        ok, _ = await world.arc("river")
        again, _ = await world.arc("river")
        second, _ = await world.arc("cave")
        third, _ = await world.arc("forest")
        return bad, ok, again, second, third

    bad, ok, again, second, third = run(scenario())
    assert [b.reason for b in bad] == ["BAD_GROUNDS", "BAD_GROUNDS"]
    assert ok.arc.status == "open" and again.reason == "EXISTS" and again.arc.id == ok.arc.id
    assert second.reason == "OK" and third.arc is None and third.reason == "ARC_LIMIT"


def test_story_rules_layer_without_weakening_and_the_screen_closes_on_doubt(tmp_path):
    with pytest.raises(ValueError):
        StoryRules.layered(
            1,
            project=(StoryRule("a", "project", "hard", "x"),),
            profile=(StoryRule("a", "profile", "desired", "weaker"),),
        )
    with pytest.raises(ValueError):
        StoryRules.layered(1, profile=(StoryRule("a", "session", "hard", "wrong level"),))

    class Doubtful:
        async def classify(self, text, rules):
            return Classification(uncertain=True)

    class Broken:
        async def classify(self, text, rules):
            raise RuntimeError("model down")

    class Invented:
        async def classify(self, text, rules):
            return Classification(hard=("no-such-rule",))

    async def scenario():
        plain = await screen(RULES, "Тихая прогулка к реке.")
        hard = await screen(RULES, "Кого-нибудь надо УБИТЬ.")
        soft = await screen(RULES, "Назначить свидание у реки.")
        out = [await screen(RULES, "Прогулка", c) for c in (Doubtful(), Broken(), Invented())]
        return plain, hard, soft, out

    plain, hard, soft, out = run(scenario())
    assert plain.allowed and not hard.allowed and hard.reason == "HARD:no-harm"
    assert soft.reason == "SOFT:no-romance" and soft.needs_operator
    assert [o.reason for o in out] == ["UNCERTAIN", "SCREEN_UNAVAILABLE", "SCREEN_UNAVAILABLE"]
    assert not any(o.allowed for o in out)


def test_a_forbidden_arc_is_refused_and_an_unsure_one_is_paused_for_the_operator(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        ground = await world.source()
        hard = await world.gm.open_arc(world.scope, "k1", [ground], "Кого-то пытка ждёт.", "b")
        soft = await world.gm.open_arc(world.scope, "k2", [ground], "Свидание на мосту.", "b")
        return hard, soft

    hard, soft = run(scenario())
    assert hard.arc is None and hard.reason == "HARD:no-harm"
    assert soft.arc.status == "paused" and soft.reason == "SOFT:no-romance"


def test_an_offer_sets_the_cooldown_and_only_one_unresolved_offer_exists(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        first = await world.offer(arc.arc.id, ground)
        too_soon = await world.offer(arc.arc.id, ground, family="promise", topic="p")
        world.clock.advance(minutes=16)
        busy = await world.offer(arc.arc.id, ground, family="promise", topic="p")
        return world, first, too_soon, busy

    world, first, too_soon, busy = run(scenario())
    assert first.offered and first.opportunity.status is S.OFFERED
    assert too_soon.reason == "COOLDOWN" and too_soon.opportunity is None
    assert busy.reason == "UNRESOLVED_LIMIT"
    arc = run(world.records(StoryArc))[0]
    assert arc.cooldown_until == NOW + timedelta(minutes=15)


def test_at_most_three_new_offers_per_two_hours_and_intensity_scales_the_pace(tmp_path):
    async def scenario(intensity):
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        world.gm.set_intensity(intensity)
        results = []
        for n in range(4):
            world.clock.advance(minutes=31)
            made = await world.offer(arc.arc.id, ground, topic=f"t{n}")
            results.append(made.reason)
            if made.offered:
                await world.gm.cancel(world.scope, made.opportunity.id)
        return results

    full = run(scenario(1.0))
    assert full == ["OK", "OK", "OK", "WINDOW_LIMIT"]
    half = run(scenario(0.5))
    assert half == ["OK", "WINDOW_LIMIT", "WINDOW_LIMIT", "WINDOW_LIMIT"]
    assert run(scenario(0.0)) == ["GM_CLOSED"] * 4


def test_two_declines_of_a_family_make_it_rest_until_the_next_session(tmp_path):
    async def scenario(tmp):
        world = await new_world(tmp)
        arc, ground = await world.arc()
        for n in range(2):
            world.clock.advance(minutes=16)
            made = await world.offer(arc.arc.id, ground, topic=f"t{n}")
            await world.gm.decline(world.scope, made.opportunity.id)
        world.clock.advance(minutes=16)
        rest = await world.offer(arc.arc.id, ground, topic="t3")
        other = await world.offer(arc.arc.id, ground, family="promise", topic="p")
        return world, arc, ground, rest, other

    world, arc, ground, rest, other = run(scenario(tmp_path))
    assert rest.reason == "FAMILY_RESTING" and other.offered
    # a different session id: the family may come back
    run(world.gm.cancel(world.scope, other.opportunity.id))
    world.clock.advance(hours=2, minutes=1)
    fresh = make_scope("session-2")
    again = run(world.gm.propose(fresh, arc.arc.id, "find", "t4", "a", "b", [ground]))
    assert again.offered


def test_the_same_situation_is_not_repeated_without_a_new_ground_and_a_pause(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        first = await world.offer(arc.arc.id, ground)
        open_again = await world.offer(arc.arc.id, ground)
        await world.gm.decline(world.scope, first.opportunity.id)
        world.clock.advance(minutes=16)
        no_new = await world.offer(arc.arc.id, ground)
        fresh = await world.source("Игрок принёс ещё один камень.")
        too_soon = await world.gm.propose(
            world.scope, arc.arc.id, "find", "stone", "a", "b", [fresh]
        )
        world.clock.advance(hours=1)
        later = await world.gm.propose(world.scope, arc.arc.id, "find", "stone", "a", "b", [fresh])
        return first, open_again, no_new, too_soon, later

    first, open_again, no_new, too_soon, later = run(scenario())
    assert open_again.reason == "ALREADY_OPEN" and no_new.reason == "NO_NEW_GROUNDS"
    assert too_soon.reason == "REPEAT_COOLDOWN" and later.offered
    assert later.opportunity.id != first.opportunity.id


def test_a_forbidden_proposal_is_stored_as_blocked_and_does_not_start_the_cooldown(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        bad = await world.offer(arc.arc.id, ground, applicability="Игрок должен убить кого-то.")
        good = await world.offer(arc.arc.id, ground, topic="other")
        return world, bad, good

    world, bad, good = run(scenario())
    assert not bad.offered and bad.reason == "HARD:no-harm"
    assert (
        bad.opportunity.status is S.BLOCKED
        and "[BLOCKED HARD:no-harm]" in bad.opportunity.boundaries
    )
    assert good.offered  # the refusal did not consume the cooldown or the window
    assert run(world.gm.character_view(world.scope))[0].opportunity_id == good.opportunity.id


def test_ungrounded_or_unknown_proposals_are_refused_without_a_record(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        own = await world.source("Я сама это придумала.", source_type="reflection")
        refused = [
            await world.offer(arc.arc.id, own),
            await world.gm.propose(world.scope, arc.arc.id, "nope", "x", "a", "b", [ground]),
            await world.gm.propose(world.scope, "arc-unknown", "find", "x", "a", "b", [ground]),
        ]
        return world, refused

    world, refused = run(scenario())
    assert [r.reason for r in refused] == ["BAD_GROUNDS", "UNKNOWN_FAMILY", "ARC_NOT_OPEN"]
    assert run(world.records(Opportunity)) == []


def test_the_character_sees_the_offer_not_the_plan_behind_it(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc(direction="Научить её просить о помощи (секретный урок).")
        made = await world.offer(arc.arc.id, ground)
        return await world.gm.character_view(world.scope), made

    view, made = run(scenario())
    text = repr(view)
    assert "секретный урок" not in text and "помощи" not in text
    assert view[0].applicability == "Странный камень у реки."
    assert made.opportunity.actual_outcome is None and made.opportunity.choice is None


def test_lifecycle_only_actual_results_become_experience(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        made = await world.offer(arc.arc.id, ground)
        oid = made.opportunity.id
        with pytest.raises(ContractError) as early:  # not accepted yet
            await world.gm.resolve(world.scope, oid, "выбор", "исход", ground)
        await world.gm.accept(world.scope, oid)
        await world.gm.start(world.scope, oid)
        own = await world.source(
            "Мы исследовали и всё получилось.", source_type="character-utterance"
        )
        with pytest.raises(ContractError) as selfmade:
            await world.gm.resolve(world.scope, oid, "выбор", "исход", own)
        with pytest.raises(ContractError) as empty:
            await world.gm.resolve(world.scope, oid, "", "исход", ground)
        no_episode_yet = await world.records(Episode, MEMORY)
        result = await world.source("Персонаж и Ника осмотрели камень: он оказался обычным.")
        done = await world.gm.resolve(
            world.scope,
            oid,
            "Осмотреть камень",
            "Камень оказался обычным.",
            result,
            ["person-nika"],
        )
        episodes = await world.records(Episode, MEMORY)
        with pytest.raises(ContractError) as again:
            await world.gm.resolve(world.scope, oid, "выбор", "исход", result)
        return early, selfmade, empty, no_episode_yet, done, episodes, again

    early, selfmade, empty, none_yet, done, episodes, again = run(scenario())
    assert early.value.error.code is ErrorCode.STALE_CONTEXT
    assert (
        selfmade.value.error.code is ErrorCode.INVALID_ARGUMENTS
        and empty.value.error.code is ErrorCode.INVALID_ARGUMENTS
    )
    assert none_yet == [] and len(episodes) == 1
    assert done.status is S.RESOLVED and done.actual_outcome == "Камень оказался обычным."
    assert (
        episodes[0].actual_outcome == "Камень оказался обычным."
        and again.value.error.code is ErrorCode.STALE_CONTEXT
    )


def test_illegal_transitions_and_expiry(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        made = await world.offer(arc.arc.id, ground)
        oid = made.opportunity.id
        await world.gm.accept(world.scope, oid)
        with pytest.raises(ContractError) as declined_after:
            await world.gm.decline(world.scope, oid)
        await world.gm.cancel(world.scope, oid)
        with pytest.raises(ContractError) as after_cancel:
            await world.gm.accept(world.scope, oid)
        world.clock.advance(minutes=16)
        second = await world.offer(arc.arc.id, ground, topic="two")
        # the owner's clock is ahead of the store's, so the record is still readable
        ahead = world.clock.now + timedelta(hours=3)
        world.gm.clock = lambda: ahead
        with pytest.raises(ContractError) as late:
            await world.gm.accept(world.scope, second.opportunity.id)
        expired = await world.gm.expire_due(world.scope)
        after_late = {r.status for r in await world.records(Opportunity)}
        view = await world.gm.character_view(world.scope)
        # once the store's own clock passes it, an expired offer is simply gone
        world.clock.advance(hours=3)
        gone = await world.records(Opportunity)
        with pytest.raises(ContractError) as hidden:
            await world.gm.accept(world.scope, second.opportunity.id)
        return declined_after, after_cancel, late, expired, after_late, view, gone, hidden, second

    d, c, late, expired, after_late, view, gone, hidden, second = run(scenario())
    assert (
        d.value.error.code is ErrorCode.STALE_CONTEXT
        and c.value.error.code is ErrorCode.STALE_CONTEXT
    )
    assert late.value.error.code is ErrorCode.EXPIRED and expired == []
    assert after_late == {S.CANCELLED, S.EXPIRED}  # the late accept already closed it
    assert view == [] and gone == []  # past their expiry both records are hidden
    # Lifecycle port retains owner metadata; terminal offer cannot be accepted again.
    assert hidden.value.error.code is ErrorCode.STALE_CONTEXT


def test_pause_cancels_waiting_offers_keeps_chosen_work_and_blocks_new_ones(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        first = await world.offer(arc.arc.id, ground)
        await world.gm.accept(world.scope, first.opportunity.id)
        world.clock.advance(minutes=16)
        cancelled_ids = await world.gm.pause(world.scope)
        blocked = await world.offer(arc.arc.id, ground, family="promise", topic="p")
        with pytest.raises(ContractError):
            await world.gm.accept(world.scope, first.opportunity.id)
        world.gm.resume()
        records = {r.id: r.status for r in await world.records(Opportunity)}
        arcs = await world.records(StoryArc)
        return first, cancelled_ids, blocked, records, arcs

    first, cancelled_ids, blocked, records, arcs = run(scenario())
    assert cancelled_ids == [] and blocked.reason == "GM_CLOSED"
    assert records[first.opportunity.id] is S.ACCEPTED and arcs[0].status == "open"


def test_pause_cancels_an_offer_nobody_has_chosen_yet(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        first = await world.offer(arc.arc.id, ground)
        cancelled = await world.gm.pause(world.scope)
        return first, cancelled, await world.gm.character_view(world.scope)

    first, cancelled, view = run(scenario())
    assert cancelled == [first.opportunity.id] and view == []


def test_stop_closes_the_game_master_and_time_alone_creates_nothing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        world.state["admitted"] = False
        world.clock.advance(hours=10)  # a long stopped period
        refused = await world.offer(arc.arc.id, ground)
        closed_arc = await world.gm.open_arc(world.scope, "x", [ground], "d", "b")
        return refused, closed_arc, await world.records(Opportunity)

    refused, closed_arc, records = run(scenario())
    assert refused.reason == "GM_CLOSED" and closed_arc.reason == "GM_CLOSED" and records == []


def test_arc_status_changes_keep_history(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        made = await world.offer(arc.arc.id, ground)
        paused = await world.gm.set_arc_status(world.scope, arc.arc.id, "paused")
        blocked = await world.offer(arc.arc.id, ground, family="promise", topic="p")
        closed = await world.gm.set_arc_status(world.scope, arc.arc.id, "closed")
        with pytest.raises(ContractError):
            await world.gm.set_arc_status(world.scope, arc.arc.id, "open")
        return made, paused, blocked, closed, await world.records(Opportunity)

    made, paused, blocked, closed, records = run(scenario())
    assert paused.version == 3 and blocked.reason == "ARC_NOT_OPEN" and closed.status == "closed"
    assert [r.id for r in records] == [made.opportunity.id]  # the lived offer is not erased


def test_dice_replay_and_filtering(tmp_path):
    first, second = Dice("s"), Dice("s")
    rolls = [
        d.choose(f"ctx{i}", ("a", "b", "c"), (1, 2, 3)).chosen
        for d in (first, second)
        for i in range(30)
    ]
    assert rolls[:30] == rolls[30:]
    assert len(set(rolls)) == 3  # all options reachable
    assert Dice("other").choose("ctx0", ("a", "b", "c")).chosen in {"a", "b", "c"}
    assert {Dice("s").choose(f"c{i}", ("a", "b"), (0, 1)).chosen for i in range(20)} == {"b"}
    value = Dice("s").strength("x", 0.2, 0.6)
    assert 0.2 <= value.value <= 0.6
    assert Dice("s").strength("x", 0.2, 0.6) == value
    with pytest.raises(ValueError):
        Dice("s").choose("c", ())
    with pytest.raises(ValueError):
        Dice("s").choose("c", ("a",), (0,))
    with pytest.raises(ValueError):
        Dice("s").strength("c", 0.6, 0.2)


def test_suggest_rolls_dice_first_then_the_model_words_the_situation(tmp_path):
    async def scenario(path):
        world = await new_world(path, [draft(), draft()])
        arc, ground = await world.arc()
        made = await world.gm.suggest(
            world.scope, arc.arc.id, [ground], ["explore", "talk"], world.generator
        )
        return world, made

    world, made = run(scenario(tmp_path))
    assert made.offered and made.opportunity.applicability == "Найдена карта без подписи."
    chosen = made.roll[0].chosen
    assert chosen in {"find", "promise"}  # 'trade' needs a capability that is not available
    system = world.provider.calls[0].messages[0].content
    assert next(f.title for f in FAMILIES if f.family_id == chosen) in system
    assert "Не задавай исход" in system
    # same seed and state in a new world: the same roll sequence
    (tmp_path / "again").mkdir()
    again = run(scenario(tmp_path / "again"))[1]
    assert [r.chosen for r in again.roll] == [r.chosen for r in made.roll]


def test_suggest_without_an_eligible_family_or_with_a_failed_model_offers_nothing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [[]])
        arc, ground = await world.arc()
        none = await world.gm.suggest(world.scope, arc.arc.id, [ground], ["fly"], world.generator)
        failed = await world.gm.suggest(
            world.scope, arc.arc.id, [ground], ["explore"], world.generator
        )
        return none, failed, await world.records(Opportunity)

    none, failed, records = run(scenario())
    assert none.reason == "NO_ELIGIBLE_FAMILY" and failed.reason == "GENERATION_FAILED"
    assert records == []


def test_work_and_test_scopes_and_personas_are_isolated(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        await world.offer(arc.arc.id, ground)
        other = make_scope("session-1", "test")
        await world.store.provision(other, __import__("gm_support").GRANTS)
        rows = await world.store.repository(Opportunity, world.gm.principal, other).list(limit=10)
        return rows

    assert run(scenario()) == []


def test_dice_results_depend_only_on_seed_and_context_not_on_earlier_rolls():
    dice = Dice("s")
    first = dice.choose("x", ("a", "b", "c"))
    dice.choose("y", ("a", "b", "c"))
    dice.strength("z", 0, 1)
    again = dice.choose("x", ("a", "b", "c"))
    assert (first.chosen, first.value) == (again.chosen, again.value)


def test_the_character_cannot_accept_while_the_gm_is_stopped_or_paused(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        arc, ground = await world.arc()
        made = await world.offer(arc.arc.id, ground)
        oid = made.opportunity.id
        world.state["admitted"] = False
        with pytest.raises(ContractError) as stopped:
            await world.gm.accept(world.scope, oid)
        world.state["admitted"] = True
        world.gm.paused = True
        with pytest.raises(ContractError) as paused:
            await world.gm.accept(world.scope, oid)
        world.gm.paused = False
        still = [r.status for r in await world.records(Opportunity)]
        accepted = await world.gm.accept(world.scope, oid)
        return stopped, paused, still, accepted

    stopped, paused, still, accepted = run(scenario())
    assert stopped.value.error.code is ErrorCode.STALE_CONTEXT
    assert paused.value.error.code is ErrorCode.STALE_CONTEXT
    assert still == [S.OFFERED] and accepted.status is S.ACCEPTED
