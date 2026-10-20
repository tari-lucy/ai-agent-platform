"""Bounded runtime workflows; one budget holder and no second call on replay."""

import asyncio

from ai_agent_application.gamemaster import Family
from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Episode, Summary
from ai_agent_contracts.persona import Opportunity
from ai_agent_contracts.states import OperationState
from composition_support import run, setup
from dev_support import answer

from ai_agent_core.m3_composition.schemas import TaskInput
from ai_agent_core.m3_composition.state import PRINCIPALS


def test_post_session_reflects_only_confirmed_dialogue_experience(tmp_path):
    """Extraction and summary run on any episode; reflection (since e8b6714) only on the
    confirmed private-dialogue experience, so a synthetic episode closes before a model call.
    The full path over a real dialogue episode: tests/integration_dialogue/test_memory_cycles.py.
    """

    async def scenario():
        env = await setup(tmp_path)
        episode_id = await env.world.episode()
        ep = await env.world.store.repository(
            Episode, PRINCIPALS[Owner.MEMORY], env.world.scope
        ).get(episode_id)
        env.world.provider.scripts.extend(
            [
                answer(proposals=[]),
                answer(content="Сводка вымышленного опыта."),
                answer(keep_state_reason="Состояние сохранить.", changes=[]),
            ]
        )
        request = TaskInput(
            kind="post_session",
            trigger_id="ended-session",
            episode_ids=[episode_id],
            source_ids=[ep.sources[0].source_id],
            period_start=ep.created_at,
            period_end=ep.created_at,
        )
        job = await env.runtime.submit(env.authority, "post-session", request)
        assert (await env.runtime.execute(job)).status == OperationState.CANCELLED
        assert len(env.world.provider.calls) == 2  # extract + summary; no reflection call
        summaries = await env.world.store.repository(
            Summary, PRINCIPALS[Owner.MEMORY], job.scope
        ).list()
        assert len(summaries) == 1 and summaries[0].analysis_id
        await env.runtime.close()

    run(scenario())


def test_background_budget_blocks_second_provider_and_retry_is_bounded(tmp_path):
    async def scenario():
        env = await setup(tmp_path)
        ep = await env.world.episode()
        rt = env.runtime
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Provider:
            async def stream(self, call):
                calls.append(call)
                entered.set()
                await release.wait()
                for event in answer(keep_state_reason="Keep", changes=[]):
                    yield event

        rt.providers = lambda _: Provider()
        request = TaskInput(kind="reflect", trigger_id="same-lived-experience", episode_ids=[ep])
        one = await rt.submit(env.authority, "one", request)
        two = await rt.submit(env.authority, "two", request)
        first = asyncio.create_task(rt.execute(one))
        await asyncio.wait_for(entered.wait(), 3)
        blocked = await rt.execute(two)
        assert blocked.status == OperationState.WAITING and len(calls) == 1
        release.set()
        assert (await first).status == OperationState.SUCCEEDED
        env.elapsed.value += 2
        env.world.clock.advance(seconds=2)
        assert (await rt.execute(blocked)).status == OperationState.SUCCEEDED
        assert len(calls) == 1
        await rt.close()

    run(scenario())


def test_gm_runtime_uses_persisted_rules_active_clock_and_registered_capability(tmp_path):
    async def scenario():
        env = await setup(tmp_path)
        ep_id = await env.world.episode()
        ep = await env.world.store.repository(
            Episode, PRINCIPALS[Owner.MEMORY], env.world.scope
        ).get(ep_id)
        rt = env.runtime
        rt.families = (Family("notice", "Заметить находку", ("m3.extract",)),)
        rt.supervisor.set_gm(env.authority, False, expected_version=rt.supervisor.snapshot.version)
        env.world.provider.scripts.extend(
            [
                answer(hard=[], soft=[], uncertain=False),
                answer(
                    applicability="Можно осмотреть след.",
                    boundaries="Без предрешённого исхода.",
                    topic_key="trace",
                ),
                answer(hard=[], soft=[], uncertain=False),
            ]
        )
        task = TaskInput(
            kind="gm",
            trigger_id="grounded-arc",
            source_ids=[ep.sources[0].source_id],
            direction="Заметить деталь.",
            boundaries="Ситуация остаётся возможностью.",
        )
        job = await rt.submit(env.authority, "gm", task)
        assert (await rt.execute(job)).status == OperationState.SUCCEEDED
        offers = await rt.store.repository(Opportunity, PRINCIPALS[Owner.GM], job.scope).list()
        assert len(offers) == 1 and offers[0].offered_active_at == 0.0
        assert len(env.world.provider.calls) == 3
        env.elapsed.value += 2
        env.world.clock.advance(seconds=2)
        assert rt.active_seconds() == 2
        rt.supervisor.set_gm(env.authority, True, expected_version=rt.supervisor.snapshot.version)
        env.elapsed.value += 2
        env.world.clock.advance(seconds=2)
        assert rt.active_seconds() == 2
        await rt.close()
        assert (await rt.state.gm(job.scope)).active_seconds == 2

    run(scenario())
