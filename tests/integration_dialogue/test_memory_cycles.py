"""Automatic private memory over authenticated real owners and synthetic model data."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_agent_application.control.ports import Clock
from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Claim, MemoryCycle, Source
from ai_agent_contracts.persona import ReflectionCheckpoint
from dev_support import answer
from integration_support import prepared
from test_history import acknowledge, prompt, send
from test_http import finish_job, post, scope_params, start

from ai_agent_core.m3_composition.service import PrivateAPIBudget
from ai_agent_core.m3_composition.state import PRINCIPALS


async def seed(env):
    await start(env)
    row = await send(env, "seed", "Вымышленная Ника любит синие зонтики.")
    await acknowledge(env, row)
    scope = env.runtime.supervisor.snapshot.scope
    source = next(
        s
        for s in await env.world.store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).list(
            limit=200
        )
        if s.source_type == "operator-message"
    )
    episodes = (await env.client.get("/api/v1/episodes", params=scope_params(scope))).json()[
        "items"
    ]
    assert len(episodes) == 1
    env.world.provider.scripts.extend(
        [
            answer(
                proposals=[
                    dict(
                        subject_id="synthetic-nika",
                        claim="Ника любит синие зонтики.",
                        proposed_type="reported",
                        confidence=0.8,
                        evidence=[dict(source_id=source.id, quote="Ника любит синие зонтики.")],
                        contradicts=[],
                    )
                ]
            ),
            answer(content="Вымышленная Ника сообщила о любви к синим зонтикам. " * 40),
            answer(keep_state_reason="Одного разговора мало для изменения характера.", changes=[]),
        ]
    )
    env.runtime.private_api_budget = PrivateAPIBudget("conn-a", 300, 24000, "utc_day", True, 8)
    return episodes[0]["id"]


def test_explicit_cycle_is_durable_attributed_and_not_rebilled(tmp_path):
    async def scenario():
        async with prepared(tmp_path, [answer(reply="Запомнила вымышленный разговор.")]) as env:
            episode = await seed(env)
            rt = env.runtime
            run = rt.supervisor.snapshot
            body = dict(
                type="session.process-memory",
                request_id="cycle-http",
                target=run.session_id,
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments=dict(episode_ids=[episode]),
            )
            result = await post(env, body)
            assert result.status_code == 200, result.text
            await finish_job(env, result.json()["operation_id"])
            cycles = await rt.store.repository(
                MemoryCycle, PRINCIPALS[Owner.MEMORY], run.scope
            ).list()
            assert len(cycles) == 1 and cycles[0].status == "completed"
            assert cycles[0].stage == "done" and len(cycles[0].record_ids) == 2
            for resource in ("claims", "summaries"):
                rows = (
                    await env.client.get("/api/v1/" + resource, params=scope_params(run.scope))
                ).json()["items"]
                assert len(rows) == 1
            claims = await rt.store.repository(Claim, PRINCIPALS[Owner.MEMORY], run.scope).list()
            assert claims[0].knowledge_type == "reported"
            checkpoints = await rt.store.repository(
                ReflectionCheckpoint, PRINCIPALS[Owner.PERSONA], run.scope
            ).list()
            assert len(checkpoints) == 1 and checkpoints[0].completed
            assert "Ника любит синие зонтики" in env.world.provider.calls[-1].messages[-1].content
            assert (await post(env, body)).json()["operation_id"] == result.json()["operation_id"]
            other = await post(
                env,
                {
                    **body,
                    "request_id": "same-batch",
                    "expected_version": rt.supervisor.snapshot.version,
                },
            )
            assert (
                other.status_code == 200
                and other.json()["operation_id"] == result.json()["operation_id"]
            )
            assert len(env.world.provider.calls) == 4
            env.world.provider.scripts.append(answer(reply="Помню вымышленный опыт."))
            await send(env, "recall", "Какие зонтики предпочитает Ника?")
            assert 'by="сводка-интерпретация"' in prompt(env)
            assert 'partial="true"' in prompt(env)
            assert 'by="утверждение-reported"' in prompt(env)
            from ai_agent_core.m3_composition.dialogue import memory
            from ai_agent_core.m3_composition.state import command

            ref = claims[0].sources[0]
            await memory(rt).set_rights(
                command(run.scope, "revoke-derived-memory"),
                ref.source_id,
                ref.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            env.world.provider.scripts.append(answer(reply="Удалённый опыт не использую."))
            await send(env, "after-revoke", "Какие зонтики предпочитает Ника?")
            assert 'by="сводка-интерпретация"' not in prompt(env)
            assert 'by="утверждение-reported"' not in prompt(env)
            await rt.close()
            await rt.open()
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await rt.daily_memory.tick()
            assert len(env.world.provider.calls) == 6

    asyncio.run(scenario())


def test_utc_boundary_schedules_without_panel_command_and_never_repeats(tmp_path):
    async def scenario():
        virtual = SimpleNamespace(
            now=datetime.now(UTC).replace(hour=23, minute=59, second=59, microsecond=0), elapsed=0.0
        )
        clock = Clock(lambda: virtual.elapsed, lambda: virtual.now)
        async with prepared(
            tmp_path, [answer(reply="Получила вымышленный опыт.")], clock=clock
        ) as env:
            await seed(env)
            rt = env.runtime
            await rt.daily_memory.tick()
            assert not await rt.daily_memory.cycles(rt.supervisor.snapshot.scope)
            virtual.now += timedelta(seconds=2)
            virtual.elapsed += 2
            env.world.clock.now = virtual.now
            # Yesterday's confirmed experience must remain processable after a new run.
            await rt.close()
            await rt.open()
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await rt.daily_memory.tick()
            assert not await rt.daily_memory.cycles(rt.supervisor.snapshot.scope)
            stopped = rt.supervisor.snapshot
            restarted = await post(
                env,
                dict(
                    type="agent.start",
                    request_id="restart-daily",
                    target="primary",
                    scope=stopped.scope.model_dump(mode="json"),
                    expected_version=stopped.version,
                    arguments={"autonomy": "autonomous"},
                ),
            )
            assert restarted.status_code == 200, restarted.text
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            cycles = await rt.daily_memory.cycles(rt.supervisor.snapshot.scope)
            assert len(cycles) == 1
            await finish_job(env, cycles[0].operation_id)
            assert (await rt.daily_memory.cycles(rt.supervisor.snapshot.scope))[
                0
            ].status == "completed"
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            assert len(env.world.provider.calls) == 4

    asyncio.run(scenario())


def test_failed_partial_batch_keeps_receipt_and_does_not_retry_paid_stages(tmp_path):
    async def scenario():
        async with prepared(tmp_path, [answer(reply="Вымышленный ответ.")]) as env:
            episode = await seed(env)
            # One valid extraction, then a closed but wrong consolidation document.
            env.world.provider.scripts[-2] = answer(wrong_schema="synthetic")
            rt = env.runtime
            result = await rt.daily_memory.enqueue([episode])
            await finish_job(env, result.operation_id, succeeded=False)
            row = (await rt.daily_memory.cycles(rt.supervisor.snapshot.scope))[0]
            assert row.status == "failed" and row.stage == "consolidate"
            assert len(row.record_ids) == 1
            calls = len(env.world.provider.calls)
            assert calls == 3
            again = await rt.daily_memory.enqueue([episode])
            assert again.status == "rejected" and len(env.world.provider.calls) == calls
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            assert len(env.world.provider.calls) == calls

    asyncio.run(scenario())
