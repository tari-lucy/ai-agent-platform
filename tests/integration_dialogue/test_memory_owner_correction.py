"""Real owner/HTTP corrections: atomic audit/replay, versions, access and STOP boundary."""

import asyncio
from datetime import timedelta

from ai_agent_application.memory import ClaimDraft, MemoryService, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Claim, Summary
from ai_agent_contracts.states import Provenance
from integration_support import prepared

from ai_agent_core.m3_composition.state import PRINCIPALS, command, put


async def seed(rt, scope, readers=None):
    now = rt.supervisor.clock.utc()
    memory = MemoryService(
        rt.store,
        PRINCIPALS[Owner.MEMORY],
        rt.registry,
        reader_access=rt.readers if readers is None else readers,
        clock=rt.supervisor.clock.utc,
    )
    source = await memory.register_source(
        command(scope, "correction-source"),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="fixture",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=now,
            content="Вымышленный Иван живёт у озера.",
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
        ),
    )
    ref = SourceRef(source_id=source.source_id, version=1, scope=scope, availability="available")
    await memory.record_claim(
        command(scope, "correction-claim"),
        ClaimDraft("correction-claim", "ivan", "Иван живёт у озера.", "reported", (ref,), 0.6),
    )
    summary = Summary(
        id="correction-summary",
        scope=scope,
        created_at=now,
        updated_at=now,
        version=1,
        period_start=now - timedelta(hours=1),
        period_end=now,
        episode_ids=[],
        sources=[ref],
        content="Исходная вымышленная сводка.",
        analysis_id="fictional-analysis",
    )
    await put(
        rt.store,
        rt.registry,
        Owner.MEMORY,
        scope,
        "correction-summary",
        summary,
        None,
        rt.readers if readers is None else readers,
    )
    return memory, source.source_id


def body(scope, *, kind="claim", target="correction-claim", request="correct-1", version=1, **data):
    return dict(
        type="memory.correct",
        request_id=request,
        scope=scope.model_dump(mode="json"),
        target=target,
        expected_version=version,
        arguments=dict(
            schema_id="memory.correct-args",
            data=dict(
                kind=kind,
                record_id=target,
                reason="Оператор уточнил вымышленный факт.",
                **data,
            ),
        ),
    )


def test_atomic_corrections_replay_history_scope_and_rights(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            rt, scope = env.runtime, env.world.scope
            memory, source_id = await seed(rt, scope)

            async def send(payload):
                return await env.client.post("/api/v1/commands", json=payload, headers=env.headers)

            original = await memory.get_source(scope, source_id)
            payload = body(scope, content="Иван живёт у реки.", confidence=0.3)
            first = await send(payload)
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "completed" and first.json()["version"] == 2
            assert (await send(payload)).json() == first.json()  # lost response retry
            repo = rt.store.repository(Claim, PRINCIPALS[Owner.MEMORY], scope)
            current = await repo.get("correction-claim")
            assert current.content == "Иван живёт у реки." and current.confidence == 0.3
            assert current.knowledge_type == "interpretation"
            assert current.correction.operator_id == "operator"
            assert current.correction.previous_version == 1
            assert current.conflict_ids == [] and current.sources[0].source_id == source_id
            old = await repo.get(current.id, version=1)
            assert old.content == "Иван живёт у озера." and old.correction is None
            assert await memory.get_source(scope, source_id) == original
            assert (await send(body(scope, content="Другая правка."))).status_code == 409
            stale = await send(body(scope, request="stale-edit", content="Старое."))
            assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
            for change in (
                dict(content="Нет повышения.", confidence=0.5),
                dict(content=" "),
                dict(),
            ):
                denied = await send(body(scope, request="bad-edit", version=2, **change))
                assert denied.status_code in (400, 422), denied.text
            summary = body(
                scope,
                kind="summary",
                target="correction-summary",
                request="correct-summary",
                content="Уточнённая сводка.",
            )
            assert (await send(summary)).status_code == 200
            row = await rt.store.repository(Summary, PRINCIPALS[Owner.MEMORY], scope).get(
                "correction-summary"
            )
            assert row.version == 2 and row.analysis_id == "fictional-analysis"
            # Source API rights govern the complete derived record including audit text.
            await memory.set_rights(
                command(scope, "close-source"),
                source_id,
                1,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            denied = await send(body(scope, request="revoked", version=2, content="Закрыто."))
            assert denied.status_code == 403
            foreign = scope.model_copy(update={"memory_space_id": "foreign"})
            assert (await send(body(foreign, content="Чужое."))).status_code == 403
            missing_csrf = await env.client.post("/api/v1/commands", json=payload)
            assert missing_csrf.status_code == 403
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert not env.world.provider.calls

    asyncio.run(scenario())


def test_failed_receipt_rolls_back_revision_and_expired_basis_stays_closed(tmp_path, monkeypatch):
    from ai_agent_adapters.storage.unit_of_work import SQLiteUnitOfWork
    from ai_agent_contracts.codec import ContractError
    from ai_agent_contracts.states import ErrorCode

    async def scenario():
        async with prepared(tmp_path) as env:
            rt, scope = env.runtime, env.world.scope
            memory, source_id = await seed(rt, scope)
            original = SQLiteUnitOfWork.complete

            async def broken(unit, receipt):
                if receipt.resource_id == "correction-claim":
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                return await original(unit, receipt)

            payload = body(scope, content="Атомарная правка.")
            monkeypatch.setattr(SQLiteUnitOfWork, "complete", broken)
            refused = await env.client.post("/api/v1/commands", json=payload, headers=env.headers)
            assert refused.status_code == 429
            repo = rt.store.repository(Claim, PRINCIPALS[Owner.MEMORY], scope)
            row = await repo.get("correction-claim")
            assert row.version == 1 and row.correction is None
            monkeypatch.setattr(SQLiteUnitOfWork, "complete", original)
            applied = await env.client.post("/api/v1/commands", json=payload, headers=env.headers)
            assert applied.status_code == 200 and applied.json()["version"] == 2
            # No edit can turn an expired basis into available memory.
            source = await memory.get_source(scope, source_id)
            expired = source.model_copy(
                update={
                    "version": 2,
                    "updated_at": rt.supervisor.clock.utc(),
                    "retention": source.retention.model_copy(
                        update={"expires_at": rt.store.clock() - timedelta(seconds=1)}
                    ),
                }
            )
            await put(
                rt.store, rt.registry, Owner.MEMORY, scope, "expired-basis", expired, 1, rt.readers
            )
            denied = await env.client.post(
                "/api/v1/commands",
                headers=env.headers,
                json=body(scope, request="expired-edit", version=2, content="Не оживёт."),
            )
            assert denied.status_code == 403
            assert not env.world.provider.calls

    asyncio.run(scenario())


def test_active_correction_refuses_and_never_changes_memory(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            rt, scope = env.runtime, env.world.scope
            await seed(rt, scope)
            start = await env.client.post(
                "/api/v1/commands",
                headers=env.headers,
                json=dict(
                    type="agent.start",
                    request_id="correction-start",
                    scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                    target=rt.supervisor.snapshot.agent_id,
                    expected_version=rt.supervisor.snapshot.version,
                    arguments={"autonomy": "observation"},
                ),
            )
            assert start.status_code == 200, start.text
            response = await env.client.post(
                "/api/v1/commands", headers=env.headers, json=body(scope, content="Поздняя правка.")
            )
            assert response.status_code == 429 and response.json()["code"] == "RESOURCE_BUSY"
            row = await rt.store.repository(Claim, PRINCIPALS[Owner.MEMORY], scope).get(
                "correction-claim"
            )
            assert row.version == 1 and row.correction is None
            assert not env.world.provider.calls

    asyncio.run(scenario())


def test_next_reply_uses_corrected_interpretation_and_lower_confidence(tmp_path):
    from dev_support import answer
    from test_history import prompt, send
    from test_http import start
    from test_memory_rights_and_control import allow_memory

    async def scenario():
        async with prepared(tmp_path, [answer(reply="Синтетический ответ.")]) as env:
            rt, scope = env.runtime, env.world.scope
            await seed(rt, scope)
            edited = await env.client.post(
                "/api/v1/commands",
                headers=env.headers,
                json=body(scope, content="Иван живёт у реки.", confidence=0.3),
            )
            assert edited.status_code == 200
            await start(env)
            allow_memory(env)
            await send(env, "after-edit", "Где живёт Иван?")
            shown = prompt(env).split('<memory by="', 1)[1]
            assert "Иван живёт у реки." in shown and "Иван живёт у озера." not in shown
            assert "исправлено-оператором-уверенность-0.3" in shown
            assert "Оператор уточнил вымышленный факт." not in shown  # audit is not a prompt
            assert len(env.world.provider.calls) == 1  # synthetic provider only

    asyncio.run(scenario())
