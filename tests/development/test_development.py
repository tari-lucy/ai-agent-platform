"""Development controller on real SQLite (M3-03): bounds, evidence, operator route, rollback."""

import pytest
from ai_agent_application.development import assess
from ai_agent_application.gamemaster import Classification, StoryRule, StoryRules
from ai_agent_application.persona.schemas import DevelopmentBound, PropertyDefinition
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.persona import StateChange
from ai_agent_contracts.states import ActivityState, ErrorCode, Provenance, RunState
from ai_agent_contracts.states import ChangeState as C
from dev_support import new_world, run


async def propose(world, prop="curiosity", value=0.77, episodes=1, **kwargs):
    ids = (
        [await world.episode() for _ in range(episodes)] if isinstance(episodes, int) else episodes
    )
    return await world.controller.submit(
        world.scope, await world.candidate(prop, value, ids, **kwargs)
    )


def values(state):
    return world_values(state)


def world_values(state):
    return state.values.data["values"]


def test_an_unknown_episode_cannot_even_be_recorded_as_evidence(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        candidate = await world.candidate("curiosity", 0.77, ["episode-ghost"])
        with pytest.raises(ContractError) as info:
            await world.controller.submit(world.scope, candidate)
        return info.value.error.code, await world.controller.history(world.scope)

    code, history = run(scenario())
    assert code is ErrorCode.INVALID_ARGUMENTS and history == []


def test_a_bounded_change_with_lived_evidence_is_ready_and_applies_as_one_new_state_version(
    tmp_path,
):
    async def scenario():
        world = await new_world(tmp_path)
        before = await world.state()
        outcome = await propose(world)
        applied = await world.controller.apply(world.scope, outcome.change.id)
        after = await world.state()
        history = await world.controller.history(world.scope)
        return before, outcome, applied, after, history

    before, outcome, applied, after, history = run(scenario())
    assert outcome.change.status is C.READY and applied.status is C.APPLIED
    assert world_values(before)["curiosity"] == 0.75 and world_values(after)["curiosity"] == 0.77
    assert after.version == before.version + 1 and applied.new_version == after.version
    assert applied.id in after.last_change_ids and applied.base_version == before.version
    assert [h.id for h in history] == [applied.id] and history[0].evidence
    assert world_values(after)["humor"] == 0.6  # nothing else moved


@pytest.mark.parametrize(
    ("value", "episodes", "error"),
    [
        (0.80, 1, "STEP_TOO_LARGE"),
        (0.75, 1, "NO_CHANGE"),
        (1.5, 1, "OUT_OF_RANGE"),
    ],
)
def test_changes_outside_the_profile_bounds_are_rejected(tmp_path, value, episodes, error):
    async def scenario():
        world = await new_world(tmp_path)
        ids = [await world.episode() for _ in range(episodes)] or ["episode-unknown"]
        return await world.controller.submit(
            world.scope, await world.candidate("curiosity", value, ids)
        )

    outcome = run(scenario())
    assert outcome.change.status is C.REJECTED and error in outcome.reasons


def test_more_independent_experience_allows_a_bigger_step_but_the_window_budget_caps_it(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        two = await propose(world, value=0.79, episodes=2)  # 0.04 = 2 × 0.02
        await world.controller.apply(world.scope, two.change.id)
        spend = await propose(world, value=0.81, episodes=1)  # 0.02 more: total 0.06 > 0.05
        world.clock.advance(days=8)
        later = await propose(world, value=0.81, episodes=1)  # the old step left the window
        return two, spend, later

    two, spend, later = run(scenario())
    assert two.change.status is C.READY
    assert spend.change.status is C.REJECTED and "WINDOW_BUDGET_EXCEEDED" in spend.reasons
    assert later.change.status is C.READY


def test_evidence_must_be_lived_independent_and_not_spent_twice(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        own = await world.episode("Я рассказала про это.", source_type="character-utterance")
        recap = await world.episode("Сводка.", provenance=Provenance.MODEL_PROPOSAL)
        self_made = await propose(world, episodes=[own, recap])
        lived = await world.episode()
        first = await propose(world, episodes=[lived])
        await world.controller.apply(world.scope, first.change.id)
        reuse = await propose(world, value=0.79, episodes=[lived], proposal_id="reuse")
        return self_made, first, reuse

    self_made, first, reuse = run(scenario())
    assert self_made.change.status is C.REJECTED
    assert (
        "NOT_ENOUGH_INDEPENDENT_EVIDENCE" in self_made.reasons
    )  # own words and recaps are no proof
    assert first.change.status is C.READY
    assert (
        reuse.change.status is C.REJECTED
    )  # a retold, already-spent experience is not new evidence


def test_a_property_that_needs_two_experiences_does_not_move_on_one(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        one = await propose(world, prop="humor", value=0.62, episodes=1)
        two = await propose(world, prop="humor", value=0.62, episodes=2)
        return one, two

    one, two = run(scenario())
    assert one.change.status is C.REJECTED and "NOT_ENOUGH_INDEPENDENT_EVIDENCE" in one.reasons
    assert two.change.status is C.READY


@pytest.mark.parametrize(
    ("prop", "value", "reason"),
    [
        ("honesty", 0.88, "IDENTITY_NEEDS_OPERATOR"),
        ("openness", 0.55, "NOT_AUTONOMOUS"),
        ("habit_note", "стала записывать находки", "TEXT_NEEDS_OPERATOR"),
    ],
)
def test_identity_non_autonomous_and_text_properties_wait_for_the_operator(
    tmp_path, prop, value, reason
):
    async def scenario():
        world = await new_world(tmp_path)
        outcome = await propose(world, prop, value)
        with pytest.raises(ContractError) as unapproved:
            await world.controller.apply(world.scope, outcome.change.id)
        state_same = (await world.state()).version
        waited = (await world.controller.history(world.scope))[0]
        approved = await world.controller.decide(world.scope, outcome.change.id, True, "decision-1")
        return outcome, unapproved, state_same, waited, approved, await world.state()

    outcome, unapproved, state_same, waited, approved, after = run(scenario())
    assert outcome.change.status is C.AWAITING_APPROVAL and reason in outcome.reasons
    assert unapproved.value.error.code is ErrorCode.STALE_CONTEXT and state_same == 1
    assert waited.status is C.AWAITING_APPROVAL  # silence is not consent
    assert approved.status is C.APPLIED and approved.operator_decision_id == "decision-1"
    assert world_values(after)[prop] == value


def test_the_operator_can_reject_and_cannot_push_an_invalid_value(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        waiting = await propose(world, "honesty", 0.85)
        rejected = await world.controller.decide(
            world.scope, waiting.change.id, False, "decision-2"
        )
        out_of_range = await propose(world, "honesty", 1.4)
        return waiting, rejected, out_of_range, (await world.state()).version

    waiting, rejected, out_of_range, version = run(scenario())
    assert rejected.status is C.REJECTED and rejected.operator_decision_id == "decision-2"
    assert out_of_range.change.status is C.REJECTED and version == 1


def test_a_stale_base_version_is_a_conflict_and_other_pending_changes_are_invalidated(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        first = await propose(world, "curiosity", 0.77)
        second = await propose(world, "humor", 0.62, episodes=2)
        await world.controller.apply(world.scope, first.change.id)
        stale = await world.controller.submit(
            world.scope,
            await world.candidate("curiosity", 0.79, [await world.episode()], base_version=1),
        )
        with pytest.raises(ContractError) as late:
            await world.controller.apply(world.scope, second.change.id)
        return stale, late, [c.status for c in await world.controller.history(world.scope)]

    stale, late, statuses = run(scenario())
    assert stale.change.status is C.CONFLICT and "STALE_BASE" in stale.reasons
    assert late.value.error.code is ErrorCode.STALE_CONTEXT  # already marked CONFLICT
    assert statuses.count(C.CONFLICT) == 2 and statuses.count(C.APPLIED) == 1


def test_duplicate_delivery_has_one_effect_and_a_different_payload_is_a_conflict(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        episode = await world.episode()
        candidate = await world.candidate("curiosity", 0.77, [episode], proposal_id="same")
        first = await world.controller.submit(world.scope, candidate)
        again = await world.controller.submit(world.scope, candidate)
        other = await world.candidate("curiosity", 0.76, [episode], proposal_id="same")
        with pytest.raises(ContractError) as conflict:
            await world.controller.submit(world.scope, other)
        await world.controller.apply(world.scope, first.change.id)
        with pytest.raises(ContractError) as twice:
            await world.controller.apply(world.scope, first.change.id)
        return (
            first,
            again,
            conflict,
            twice,
            await world.state(),
            await world.controller.history(world.scope),
        )

    first, again, conflict, twice, state, history = run(scenario())
    assert again.duplicate and again.change.id == first.change.id
    assert conflict.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert twice.value.error.code is ErrorCode.STALE_CONTEXT
    assert state.version == 2 and len(history) == 1


def test_autonomous_changes_wait_while_paused_stopped_or_observing(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        outcome = await propose(world)
        results = {}
        for name, change in (
            ("paused", lambda: setattr(world.control, "activity", ActivityState.PAUSED)),
            ("stopped", lambda: setattr(world.control, "run_state", RunState.STOPPED)),
            ("observation", lambda: setattr(world.control, "autonomy", "observation")),
            ("unadmitted", lambda: setattr(world.control, "admitted", False)),
        ):
            world.control.activity, world.control.run_state = ActivityState.OPEN, RunState.ACTIVE
            world.control.autonomy, world.control.admitted = "autonomous", True
            change()
            with pytest.raises(ContractError) as info:
                await world.controller.apply(world.scope, outcome.change.id)
            results[name] = info.value.error.code
        waiting = (await world.controller.history(world.scope))[0].status
        world.control.activity, world.control.run_state = ActivityState.OPEN, RunState.ACTIVE
        world.control.autonomy, world.control.admitted = "autonomous", True
        applied = await world.controller.apply(world.scope, outcome.change.id)
        return results, waiting, applied

    results, waiting, applied = run(scenario())
    assert set(results.values()) == {ErrorCode.STALE_CONTEXT} and waiting is C.READY
    assert applied.status is C.APPLIED


def test_an_operator_correction_is_a_separate_explicit_command_even_when_paused(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        waiting = await propose(world, "honesty", 0.88)
        world.control.activity = ActivityState.PAUSED
        return await world.controller.decide(world.scope, waiting.change.id, True, "decision-3")

    assert run(scenario()).status is C.APPLIED


def test_story_rules_reject_forbidden_development_and_unsure_cases_go_to_the_operator(tmp_path):
    rules = StoryRules.layered(
        1, project=(StoryRule("no-cruelty", "project", "hard", "x", ("жесток",)),)
    )

    class Unsure:
        async def classify(self, text, rules):
            return Classification(uncertain=True)

    async def scenario():
        world = await new_world(tmp_path, rules=rules)
        bad = await propose(world, interpretation="Научилась получать удовольствие от ЖЕСТОКОСТИ.")
        world.controller.classifier = Unsure()
        unsure = await propose(world)
        return bad, unsure

    bad, unsure = run(scenario())
    assert bad.change.status is C.REJECTED and "HARD:no-cruelty" in bad.reasons
    assert unsure.change.status is C.AWAITING_APPROVAL and "UNCERTAIN" in unsure.reasons


def test_too_uncertain_interpretations_are_rejected(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        return await propose(world, uncertainty=0.9)

    outcome = run(scenario())
    assert outcome.change.status is C.REJECTED and "TOO_UNCERTAIN" in outcome.reasons


def test_rollback_is_a_previewed_compensating_change_that_keeps_history(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        a = await propose(world, value=0.77)
        await world.controller.apply(world.scope, a.change.id)
        b = await propose(world, value=0.79, proposal_id="second")
        await world.controller.apply(world.scope, b.change.id)
        pending = await propose(world, "humor", 0.62, episodes=2)
        preview = await world.controller.preview_rollback(world.scope, a.change.id)
        with pytest.raises(ContractError) as wrong:
            await world.controller.rollback(world.scope, a.change.id, "decision-4", "0" * 64)
        compensations = await world.controller.rollback(
            world.scope, a.change.id, "decision-4", preview.digest
        )
        state = await world.state()
        history = await world.controller.history(world.scope)
        with pytest.raises(ContractError) as again:
            await world.controller.preview_rollback(world.scope, a.change.id)
        return a, b, pending, preview, wrong, compensations, state, history, again

    a, b, pending, preview, wrong, compensations, state, history, again = run(scenario())
    assert preview.chain == (b.change.id, a.change.id) and preview.restore_value == 0.75
    assert preview.current_value == 0.79 and pending.change.id in preview.invalidated_pending
    assert wrong.value.error.code is ErrorCode.STALE_CONTEXT
    assert world_values(state)["curiosity"] == 0.75 and state.version == 4
    assert {c.revision_of for c in compensations} == {a.change.id, b.change.id}
    assert all(
        c.operator_decision_id == "decision-4" and c.status is C.APPLIED for c in compensations
    )
    originals = [h for h in history if h.id in (a.change.id, b.change.id)]
    assert all(o.status is C.APPLIED for o in originals)  # the past is not rewritten
    assert again.value.error.code is ErrorCode.STALE_CONTEXT
    # the dependent pending proposal was revalidated, not left to apply blindly
    assert next(h for h in history if h.id == pending.change.id).status is C.CONFLICT


def test_a_rolled_back_change_no_longer_spends_the_window_budget(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        first = await propose(world, value=0.79, episodes=2)
        await world.controller.apply(world.scope, first.change.id)
        blocked = await propose(world, value=0.81, episodes=1, proposal_id="b")
        preview = await world.controller.preview_rollback(world.scope, first.change.id)
        await world.controller.rollback(world.scope, first.change.id, "d", preview.digest)
        free = await propose(world, value=0.77, episodes=1, proposal_id="c")
        return blocked, free

    blocked, free = run(scenario())
    assert blocked.change.status is C.REJECTED and free.change.status is C.READY


def test_rollback_needs_an_applied_original_change(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        ready = await propose(world)
        with pytest.raises(ContractError) as not_applied:
            await world.controller.preview_rollback(world.scope, ready.change.id)
        with pytest.raises(ContractError) as unknown:
            await world.controller.preview_rollback(world.scope, "chg-missing")
        return not_applied, unknown

    not_applied, unknown = run(scenario())
    assert not_applied.value.error.code is ErrorCode.STALE_CONTEXT
    assert unknown.value.error.code is ErrorCode.STALE_CONTEXT


def test_cancelling_a_pending_change_leaves_the_state_alone(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        ready = await propose(world)
        cancelled = await world.controller.cancel(world.scope, ready.change.id)
        with pytest.raises(ContractError):
            await world.controller.apply(world.scope, ready.change.id)
        return cancelled, (await world.state()).version

    cancelled, version = run(scenario())
    assert cancelled.status is C.CANCELLED and version == 1


def test_history_is_per_property_and_per_scope(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        a = await propose(world)
        h = await propose(world, "humor", 0.62, episodes=2)
        test_scope = world.scope.model_copy(update={"memory_mode": "test", "memory_space_id": "t"})
        await world.store.provision(test_scope, __import__("dev_support").GRANTS)
        return (
            [c.id for c in await world.controller.history(world.scope, "humor")],
            len(await world.controller.history(world.scope)),
            await world.store.repository(StateChange, world.controller.principal, test_scope).list(
                limit=5
            ),
            a,
            h,
        )

    humor, total, other, a, h = run(scenario())
    assert humor == [h.change.id] and total == 2 and other == []


def test_pure_assessment_routes_and_limits():
    scale = PropertyDefinition(
        name="x", layer="dispositions", title="x", kind="scale", minimum=0, maximum=1
    )
    bound = DevelopmentBound(
        name="x",
        autonomous=True,
        max_step_per_episode=0.02,
        max_step_per_window=0.05,
        window_days=7,
        min_independent_episodes=1,
    )
    assert assess(scale, bound, 0.5, 0.52, 1, 0).route == "autonomous"
    assert "STEP_TOO_LARGE" in assess(scale, bound, 0.5, 0.53, 1, 0).errors
    assert "WINDOW_BUDGET_EXCEEDED" in assess(scale, bound, 0.5, 0.52, 1, 0.04).errors
    assert "NOT_ENOUGH_INDEPENDENT_EVIDENCE" in assess(scale, bound, 0.5, 0.52, 0, 0).errors
    assert assess(scale, None, 0.5, 0.52, 1, 0).reasons == ("NOT_AUTONOMOUS",)
    assert assess(scale, bound, 0.5, "text", 1, 0).errors == ("VALUE_TYPE",)
    assert assess(scale, bound, 0.5, 0.5, 1, 0).errors == ("NO_CHANGE",)


def test_evidence_from_the_wrong_kind_of_experience_for_the_scope_is_rejected(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, mode="test")
        real = await world.episode(
            mode_provenance="observation"
        )  # not a simulation, in a test scope
        return await propose(world, episodes=[real])

    outcome = run(scenario())
    assert outcome.change.status is C.REJECTED and "EPISODE_PROVENANCE" in outcome.reasons


def test_a_state_changed_behind_the_controllers_back_blocks_the_apply(tmp_path):
    async def scenario():
        world = await new_world(tmp_path)
        ready = await propose(world)
        state = await world.state()
        bumped = world.controller._revised(
            state,
            world.scope,
            version=state.version + 1,
            state_version=state.version + 1,
            updated_at=world.clock.now.isoformat(),
        )
        command = world.command()
        async with world.store.transaction(
            world.controller.principal, world.scope, command
        ) as unit:
            await unit.put(bumped, expected_version=state.version)
            from ai_agent_contracts.api import Receipt

            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=world.scope,
                    status="completed",
                    resource_id=bumped.id,
                    version=2,
                )
            )
        with pytest.raises(ContractError) as info:
            await world.controller.apply(world.scope, ready.change.id)
        return info.value.error.code, (await world.controller.history(world.scope))[0].status

    code, status = run(scenario())
    assert code is ErrorCode.VERSION_CONFLICT and status is C.CONFLICT
