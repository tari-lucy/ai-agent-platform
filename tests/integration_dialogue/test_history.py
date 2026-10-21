"""Owner integration: durable bounded history, scope isolation and revoked sources."""

import asyncio
from datetime import timedelta

import pytest
from ai_agent_adapters.llm.mock import Pause
from ai_agent_application.memory import SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.states import Provenance
from dev_support import answer
from integration_support import prepared
from test_http import finish_job, post, start

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.history import PrivateHistory
from ai_agent_core.m3_composition.state import PRINCIPALS, command


async def send(env, name, text, *, succeeded=True):
    env.world.clock.now += timedelta(milliseconds=10)
    run = env.runtime.supervisor.snapshot
    result = await post(
        env,
        dict(
            type="dialogue.message",
            request_id=name,
            target=run.session_id,
            scope=run.scope.model_dump(mode="json"),
            arguments={"text": text},
        ),
    )
    assert result.status_code == 200, result.text
    await finish_job(env, result.json()["operation_id"], succeeded=succeeded)
    rows = await env.runtime.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], run.scope).list(
        limit=200
    )
    return next((u for u in rows if u.operation_id == result.json()["operation_id"]), None)


async def acknowledge(env, row):
    env.world.clock.now += timedelta(milliseconds=10)
    run = env.runtime.supervisor.snapshot
    result = await post(
        env,
        dict(
            type="dialogue.acknowledge",
            request_id="ack-" + row.id,
            target=row.id,
            scope=run.scope.model_dump(mode="json"),
            expected_version=row.version,
            arguments={"revision": row.revision},
        ),
    )
    assert result.status_code == 200, result.text


def prompt(env):
    return env.world.provider.calls[-1].messages[-1].content


def test_real_owner_context_contains_ordered_delivered_history_after_restart(tmp_path):
    async def scenario():
        async with prepared(
            tmp_path,
            [answer(reply="Синтетический ответ Альфа."), answer(reply="Синтетический ответ Бета.")],
        ) as env:
            await start(env)
            first = await send(env, "first", "Вымышленный ввод Альфа.")
            await acknowledge(env, first)
            await env.runtime.close()
            await env.runtime.open()
            assert env.runtime.supervisor.snapshot.state.value == "STOPPED"
            run = env.runtime.supervisor.snapshot
            restarted = await post(
                env,
                dict(
                    type="agent.start",
                    request_id="restart-history",
                    target="primary",
                    scope=run.scope.model_dump(mode="json"),
                    expected_version=run.version,
                    arguments={"autonomy": "autonomous"},
                ),
            )
            assert restarted.status_code == 200, restarted.text
            second = await send(env, "second", "Продолжим разговор без общего ключевого слова.")
            assert {r.source_id for r in first.sources}.issubset(
                {r.source_id for r in second.sources}
            )
            body = prompt(env)
            assert '<turn role="person" by="оператор">Вымышленный ввод Альфа.</turn>' in body
            assert '<turn role="character" by="персонаж">Синтетический ответ Альфа.</turn>' in body
            assert (
                body.index('<turn role="person"')
                < body.index('<turn role="character"')
                < body.index("<incoming ")
            )
            assert len(env.world.provider.calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("removal", ["unacknowledged", "rights", "delete", "expired"])
def test_unconfirmed_or_unavailable_text_never_reappears_in_history_or_recall(tmp_path, removal):
    async def scenario():
        async with prepared(
            tmp_path, [answer(reply="Маяк REPLY_PRIVATE_MARKER."), answer(reply="Готово.")]
        ) as env:
            await start(env)
            first = await send(env, "first", "Маяк INPUT_PRIVATE_MARKER.")
            if removal != "unacknowledged":
                await acknowledge(env, first)
            source = await memory(env.runtime).get_source(first.scope, first.sources[0].source_id)
            mem = memory(env.runtime)
            if removal == "rights":
                await mem.set_rights(
                    command(first.scope, "revoke"),
                    source.id,
                    source.version,
                    read_allowed=True,
                    api_transfer_allowed=False,
                    public_disclosure_allowed=False,
                )
            elif removal == "delete":
                await mem.delete_source(command(first.scope, "remove"), source.id, source.version)
            elif removal == "expired":
                # Exercise the authoritative expiry filter while the derived index still exists.
                from ai_agent_core.m3_composition.state import put

                updated = source.model_copy(
                    update={
                        "version": source.version + 1,
                        "retention": source.retention.model_copy(
                            update={
                                "expires_at": env.runtime.supervisor.clock.utc()
                                + timedelta(seconds=1)
                            }
                        ),
                        "updated_at": env.runtime.supervisor.clock.utc(),
                    }
                )
                await put(
                    env.runtime.store,
                    env.runtime.registry,
                    Owner.MEMORY,
                    source.scope,
                    "expire",
                    updated,
                    source.version,
                    env.runtime.readers,
                )
                env.world.clock.now = env.runtime.supervisor.clock.utc() + timedelta(seconds=2)
                # The storage uses a separate injected clock for retention.
                env.runtime.store.clock = lambda: env.world.clock.now
            await send(env, "second", "Что известно про маяк?")
            body = prompt(env)
            assert "REPLY_PRIVATE_MARKER" not in body
            if removal != "unacknowledged":
                assert "INPUT_PRIVATE_MARKER" not in body
            else:
                assert "INPUT_PRIVATE_MARKER" in body

    asyncio.run(scenario())


def test_recent_window_is_bounded_and_isolated(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            mem, scope = memory(env.runtime), env.world.scope
            refs = []
            for i in range(12):
                env.world.clock.now += timedelta(milliseconds=10)
                result = await mem.register_source(
                    command(scope, f"src-{i}"),
                    SourceDraft(
                        provenance=Provenance.SIMULATION
                        if scope.memory_mode == "test"
                        else Provenance.OPERATOR_INPUT,
                        source_type="operator-message",
                        adapter_id="dialogue",
                        adapter_version="1",
                        occurred_at=env.runtime.supervisor.clock.utc(),
                        content=f"Вымышленная реплика {i:02}.",
                        read_allowed=True,
                        api_transfer_allowed=True,
                        public_disclosure_allowed=False,
                        external_id=f"history-{i}",
                    ),
                )
                refs.append(
                    SourceRef(
                        source_id=result.source_id,
                        version=result.version,
                        scope=scope,
                        availability="available",
                    )
                )
            history = PrivateHistory(env.runtime)
            rows = await history.recent(scope, refs[-1], 8)
            assert [r.text for r in rows] == [f"Вымышленная реплика {i:02}." for i in range(3, 11)]
            other = scope.model_copy(
                update={"memory_mode": "test", "memory_space_id": "other-history"}
            )
            assert await PrivateHistory(env.runtime).recent(other, refs[-1], 8) == ()

    asyncio.run(scenario())


def test_history_revoked_during_generation_cannot_save_late_reply(tmp_path):
    async def scenario():
        script = answer(reply="Поздний ответ.")
        script.insert(1, Pause(2.0))  # wide enough to revoke while generating, even under load
        async with prepared(tmp_path, [answer(reply="Первый ответ."), script]) as env:
            await start(env)
            first = await send(env, "first", "Синтетическая история.")
            await acknowledge(env, first)
            run = env.runtime.supervisor.snapshot
            result = await post(
                env,
                dict(
                    type="dialogue.message",
                    request_id="late",
                    target=run.session_id,
                    scope=run.scope.model_dump(mode="json"),
                    arguments={"text": "Продолжим."},
                ),
            )
            assert result.status_code == 200
            # Wait for the second model call itself, not a fixed 3 s (its job starts on a tick).
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while len(env.world.provider.calls) < 2 and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert len(env.world.provider.calls) == 2, "the late reply never reached the model"
            ref = first.sources[0]
            await memory(env.runtime).set_rights(
                command(run.scope, "revoke-history"),
                ref.source_id,
                ref.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            outcome = await finish_job(env, result.json()["operation_id"], succeeded=False)
            assert outcome.status.value != "SUCCEEDED"
            rows = await env.runtime.store.repository(
                Utterance, PRINCIPALS[Owner.GATEWAY], run.scope
            ).list()
            assert not any(r.content == "Поздний ответ." for r in rows)

    asyncio.run(scenario())
