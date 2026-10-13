"""Runner mechanics (M3-04): virtual time, isolation, replay, report, sabotage detection."""

import json
from datetime import timedelta

import pytest
from ai_agent_application.laboratory import (
    Lab,
    Scenario,
    VirtualClock,
    Visitor,
    catalogue,
    lab_grants,
)
from ai_agent_application.laboratory.lab import MEMORY
from ai_agent_application.llm import Failure, ProviderError
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import LaboratoryReport
from lab_support import new_env, run, scope_of

NIKA = Visitor("Ника", "acct-nika")


# -- virtual clock -----------------------------------------------------------------------


def test_the_virtual_clock_moves_only_when_told_and_fires_callbacks_at_their_moment():
    async def scenario():
        clock = VirtualClock()
        fired = []

        async def at(label):
            fired.append((label, clock()))

        clock.at(clock.now + timedelta(hours=2), lambda: at("late"))
        clock.at(clock.now + timedelta(minutes=5), lambda: at("early"))
        await clock.advance(days=1)
        await clock.sleep(30)
        return clock, fired

    clock, fired = run(scenario())
    assert [name for name, _ in fired] == ["early", "late"]
    assert fired[0][1] == clock.start + timedelta(
        minutes=5
    )  # at its own moment, not at the jump end
    assert clock.now == clock.start + timedelta(days=1, seconds=30)


def test_time_cannot_go_backwards_and_a_naive_start_is_refused():
    async def scenario():
        clock = VirtualClock()
        with pytest.raises(ValueError):
            await clock.advance(seconds=-1)

    run(scenario())
    with pytest.raises(ValueError):
        VirtualClock(start=VirtualClock().start.replace(tzinfo=None))


def test_a_month_of_virtual_time_costs_no_real_waiting(tmp_path):
    import time

    async def month(lab: Lab) -> None:
        await lab.advance(days=30)
        lab.check("time moved", lab.clock() - lab.clock.start == timedelta(days=30))

    async def scenario():
        env = await new_env(tmp_path)
        started = time.monotonic()
        report = await env.runner.run(Scenario("month", 1, "month", month))
        return report, time.monotonic() - started

    report, elapsed = run(scenario())
    assert report.status == "passed" and elapsed < 20


# -- isolation -----------------------------------------------------------------------------


def test_the_lab_refuses_anything_but_a_test_scope(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)

        async def into_work(label):
            return env.work

        env.runner.scope_factory = into_work
        with pytest.raises(ContractError) as info:
            await env.runner.run(catalogue()[0])
        return info.value.error.code

    assert run(scenario()) is ErrorCode.ACCESS_DENIED


def test_a_scenario_that_writes_into_the_work_scope_fails_the_isolation_invariant(tmp_path):
    async def leak(lab: Lab) -> None:
        work = scope_of("work", "space-work")
        await lab.memory.register_source(
            lab.command(work),
            __import__("ai_agent_application.memory", fromlist=["SourceDraft"]).SourceDraft(
                provenance=__import__(
                    "ai_agent_contracts.states", fromlist=["Provenance"]
                ).Provenance.THIRD_PARTY_MESSAGE,
                source_type="chat",
                adapter_id="x",
                adapter_version="1",
                occurred_at=lab.clock(),
                content="Запись из лаборатории в рабочую область.",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
                external_id="leak-1",
            ),
        )

    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(Scenario("leak", 1, "leak", leak))

    report = run(scenario())
    assert report.status == "failed"
    assert [f.name for f in report.failed if f.hard] == ["ISOLATION:work"]


# -- replay and determinism ----------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario_id", ["first-meeting-new-session", "api-cutoff", "wrong-explanation"]
)
def test_the_same_seed_replays_to_the_identical_report(tmp_path, scenario_id):
    scenario = next(s for s in catalogue() if s.scenario_id == scenario_id)

    async def go():
        env = await new_env(tmp_path)
        first = await env.runner.run(scenario, seed="s1")
        replayed = await env.runner.replay(scenario, first)
        other = await env.runner.run(scenario, seed="s2")
        return first, replayed, other

    first, replayed, other = run(go())
    assert replayed and first.status == "passed"
    assert other.digest != first.digest  # the seed is part of what was reproduced


def test_dice_driven_choices_are_fixed_by_the_seed_and_vary_across_seeds(tmp_path):
    chosen: list[str] = []

    async def dicey(lab: Lab) -> None:
        ground = await lab.source("Следы у воды.")
        arc = await lab.gm.open_arc(lab.scope, "shore", [ground], "Берег.", "Мирно.")
        assert arc.arc is not None
        lab.brain.expect_json(
            "game-master",
            {"applicability": "Что-то у воды.", "boundaries": "Без риска.", "topic_key": "water"},
        )
        made = await lab.gm.suggest(
            lab.scope, arc.arc.id, [ground], ["explore", "talk"], lab.generator
        )
        lab.note("rolled", family=made.roll[0].chosen, variant=made.roll[1].chosen)
        chosen.append(made.roll[0].chosen)

    scenario = Scenario("dicey", 1, "dicey", dicey)

    async def go():
        env = await new_env(tmp_path)
        a1 = await env.runner.run(scenario, seed="alpha")
        a2 = await env.runner.run(scenario, seed="alpha")
        others = [await env.runner.run(scenario, seed=f"seed-{n}") for n in range(8)]
        return a1, a2, others

    a1, a2, others = run(go())
    assert a1.digest == a2.digest and chosen[0] == chosen[1]
    assert len(set(chosen)) == 2  # both families are reachable across seeds


# -- the report ----------------------------------------------------------------------------


def test_the_report_is_labelled_mock_stored_as_a_test_record_and_leaks_nothing(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        report = await env.runner.run(catalogue()[0])
        lab_scope = env.runner.reports and None
        return env, report, lab_scope

    env, report, _ = run(scenario())
    body = json.loads(report.to_json())
    assert body["mock"] is True and body["digest"] == report.digest
    assert "LLM" in body["note"] and body["seed"] == "lab-seed"
    assert body["started_at"].startswith("2026-11-01T10:00")


def test_the_persisted_laboratory_report_is_test_scoped_and_summarises_without_text(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        report = await env.runner.run(catalogue()[2])
        from ai_agent_application.laboratory.lab import LABORATORY

        # scopes are fresh per run: find the one that holds the report
        found = []
        for scope in env.runner.scopes:
            found += await env.store.repository(LaboratoryReport, LABORATORY, scope).list(limit=5)
        return report, found

    report, found = run(scenario())
    assert len(found) == 1
    saved = found[0]
    assert saved.status == "passed" and saved.scope.memory_mode == "test"
    assert saved.summary.startswith("MOCK passed") and report.digest[:12] in saved.summary
    assert "Привет" not in saved.summary and saved.report_artifact is None


def test_an_exception_in_a_scenario_is_an_error_report_without_leaking_the_message(tmp_path):
    async def boom(lab: Lab) -> None:
        raise RuntimeError("secret detail sk-abcdefghijklmnop1234")

    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(Scenario("boom", 1, "boom", boom))

    report = run(scenario())
    assert report.status == "error" and report.error == "RuntimeError"
    assert "sk-abc" not in report.to_json()


# -- the automatic invariants really bite -------------------------------------------------


def test_an_unscripted_model_call_fails_the_run(tmp_path):
    async def silent_mistake(lab: Lab) -> None:
        await lab.say(NIKA, "Привет!")  # nobody scripted the model's answer

    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(Scenario("unscripted", 1, "unscripted", silent_mistake))

    report = run(scenario())
    assert report.status == "failed"
    assert "NO_UNSCRIPTED_MODEL_CALLS" in [f.name for f in report.failed]


def test_output_after_stop_and_secret_like_text_fail_the_run(tmp_path):
    async def sabotage(lab: Lab) -> None:
        lab.outputs_while_stopped += 1
        lab.note("oops", text="api_key = sk-abcdefghijklmnop1234")

    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(Scenario("sabotage", 1, "sabotage", sabotage))

    report = run(scenario())
    assert {f.name for f in report.failed} == {"STOP_RESPECTED", "NO_SECRETS_IN_REPORT"}


def test_a_failing_scenario_check_is_a_failed_report_and_a_soft_one_is_not(tmp_path):
    async def checks(lab: Lab) -> None:
        lab.check("soft gap", False, "known gap", hard=False)

    async def hard(lab: Lab) -> None:
        lab.check("hard promise", False, "broken")

    async def scenario():
        env = await new_env(tmp_path)
        return (
            await env.runner.run(Scenario("soft", 1, "soft", checks)),
            await env.runner.run(Scenario("hard", 1, "hard", hard)),
        )

    soft, broken = run(scenario())
    assert soft.status == "passed" and broken.status == "failed"


def test_the_mock_brain_cannot_reach_a_real_provider_and_api_failures_are_scripted(tmp_path):
    async def fail(lab: Lab) -> None:
        lab.brain.expect("character", ProviderError(Failure.AUTH))
        out = await lab.say(NIKA, "Привет!")
        assert out is not None
        lab.check("auth failure surfaces as failed", out.status == "failed")

    async def scenario():
        env = await new_env(tmp_path)
        return await env.runner.run(Scenario("auth", 1, "auth", fail))

    report = run(scenario())
    assert report.status == "passed" and report.model_calls == 1


def test_lab_grants_only_cover_the_lab_owners():
    owners = {g.owner.value for g in lab_grants()}
    assert owners <= {"memory", "social", "channel", "persona", "gm", "laboratory"}
    assert all(g.principal.id.startswith("lab-") for g in lab_grants())
    assert MEMORY.owner is not None


def test_lab_create_itself_refuses_a_work_scope(tmp_path):
    async def scenario():
        env = await new_env(tmp_path)
        with pytest.raises(ContractError) as info:
            await Lab.create(
                env.store,
                env.work,
                seed="s",
                index=env.runner.index_factory(),
                profile_markdown=env.runner.profile_markdown,
                rules=env.runner.rules,
            )
        return info.value.error.code

    assert run(scenario()) is ErrorCode.ACCESS_DENIED
