"""An archived claim/summary obeys raw-source TTL, deletion, revoked transfer and new versions.

Real owners/HTTP on a temporary SQLite with a virtual clock, the real independent ledger and the
retention owner. Records sit outside the recent windows, so only the archive lookup can reach
them; what the scripted model SAW is read from its request.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_application.memory import SourceDraft
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Claim, Source, Summary
from ai_agent_contracts.states import Provenance
from dev_support import answer
from integration_support import prepared
from test_history import send
from test_http import start
from test_long_term_archive import claim, summary
from test_memory_lifecycle import plan, post
from test_memory_rights_and_control import allow_memory, fragments

from ai_agent_core.m3_composition import long_term
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command

OPERATOR = Principal("operator", "operator")


@asynccontextmanager
async def world(tmp_path, replies):
    async with prepared(tmp_path, [answer(reply=text) for text in replies]) as env:
        rt, scope = env.runtime, env.world.scope
        # Explicit fixture matrix as in the lifecycle acceptance, never runtime widening.
        await rt.store.provision(
            scope,
            (
                *env.grants,
                Grant(PRINCIPALS[Owner.GATEWAY], Owner.MEMORY, read=True),
                *(
                    Grant(OPERATOR, owner, read=True, api_transfer=True)
                    for owner in (
                        Owner.WORKSPACE,
                        Owner.PERSONA,
                        Owner.MEMORY,
                        Owner.GATEWAY,
                        Owner.CHANNEL,
                        Owner.JOBS,
                        Owner.LLM,
                        Owner.GM,
                    )
                ),
            ),
        )
        physical = SQLitePhysicalStore(rt.store)
        await physical.install()
        ledger = SQLiteDeletionLedger(tmp_path / "independent-ledger.sqlite")
        rt.retention = RetentionService(
            rt.store,
            PRINCIPALS[Owner.MEMORY],
            memory(rt),
            physical,
            ledger,
            index=env.index,
            clock=rt.store.clock,  # the virtual clock that also decides storage TTL
        )
        env.physical, env.ledger = physical, ledger
        try:
            await start(env)
            allow_memory(env)
            yield env
        finally:
            ledger.close()


async def fact(env, name, text, *, age=timedelta(0)) -> SourceRef:
    """A person's statement that occurred `age` ago; the TTL counts from the occurrence."""
    env.world.clock.now += timedelta(milliseconds=10)
    rt, scope = env.runtime, env.world.scope
    made = await memory(rt).register_source(
        command(scope, "src-" + name),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="fixture",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=rt.store.clock() - age,
            content=text,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
        ),
    )
    return SourceRef(
        source_id=made.source_id, version=made.version, scope=scope, availability="available"
    )


async def noise(env):
    """Newer, unrelated records that fill both recent windows."""
    rt, scope = env.runtime, env.world.scope
    ref = await fact(env, "noise", "Ника: обсуждали погоду и порт.")
    for n in range(long_term.CLAIM_WINDOW + 1):
        env.world.clock.now += timedelta(milliseconds=10)
        await claim(rt, scope, f"noise-{n}", f"По словам Ники, тема {n}: погода.", ref)
    for n in range(long_term.SUMMARY_WINDOW):
        env.world.clock.now += timedelta(milliseconds=10)
        await summary(rt, scope, f"noise-{n}", f"Сводка {n}: погода и порт.", ref)


async def power(env, name, operation):
    """STOP or START through HTTP; deletion is planned only while the agent is stopped."""
    run = env.runtime.supervisor.snapshot
    body = dict(
        type="agent." + operation,
        request_id=name,
        target="primary",
        scope=run.scope.model_dump(mode="json"),
    )
    if operation == "start":
        body.update(expected_version=run.version, arguments={"autonomy": "autonomous"})
        for _ in range(200):  # the stopped run releases its resources asynchronously
            response = await post(env, body)
            if response.status_code != 429:
                break
            await asyncio.sleep(0.05)
    else:
        response = await env.client.post(
            "/api/v1/agents/primary/stop", headers=env.headers, json=body
        )
    assert response.status_code == 200, response.text


async def shown(env, name, question) -> str:
    await send(env, name, question)
    return " ".join(m["text"] for m in fragments(env))


async def readable(env, model, record_id) -> bool:
    repo = env.runtime.store.repository(model, OPERATOR, env.world.scope)
    return await repo.get(record_id, purpose="api_transfer") is not None


def test_archived_memory_lives_no_longer_than_its_raw_source(tmp_path):
    async def scenario():
        out: dict = {}
        async with world(tmp_path, [f"Ответ {n}." for n in range(3)]) as env:
            rt, scope = env.runtime, env.world.scope
            # 30-day conversation TTL counted from the occurrence: 20 minutes are left
            old = await fact(
                env,
                "old",
                "Ника: мой талисман — бирюзовый фонарик.",
                age=timedelta(days=30) - timedelta(minutes=20),
            )
            await claim(rt, scope, "old", "По словам Ники, её талисман — бирюзовый фонарик.", old)
            await summary(rt, scope, "old", "По словам Ники, бирюзовый фонарик — талисман.", old)
            await noise(env)
            question = "Какой у меня талисман?"
            out["before_ttl"] = await shown(env, "q1", question)
            env.world.clock.now += timedelta(minutes=25)  # past the raw TTL, before any sweep
            out["after_ttl"] = await shown(env, "q2", question)
            out["readable"] = (
                await readable(env, Claim, "claim-old"),
                await readable(env, Summary, "summary-old"),
            )
            out["stored_derivatives"] = await env.physical.durable_derivatives(scope, old.source_id)
            swept = await rt.retention.sweep(scope)
            out["swept"] = (old.source_id in swept.expired, old.source_id in swept.unconsolidated)
            found = await env.index.search(
                scope, question, None, None, limit=24, now=rt.supervisor.clock.utc(), prefix=True
            )
            out["indexed"] = [c.source_id for c in found.lexical]
            out["after_sweep"] = await shown(env, "q3", question)
            stub = await env.physical.source(scope, old.source_id)
            out["raw"] = (stub.availability, stub.has_content)
            out["old_id"] = old.source_id
        return out

    out = asyncio.run(scenario())
    assert "бирюзовый фонарик" in out["before_ttl"]  # older than both windows, still found
    assert "бирюзовый" not in out["after_ttl"] and "бирюзовый" not in out["after_sweep"]
    assert out["readable"] == (False, False)  # storage hides derived records of expired raw
    assert out["stored_derivatives"] == 2  # kept as records, not served (see the report)
    assert out["swept"] == (True, False)  # expired on schedule; derivatives exist
    assert out["old_id"] not in out["indexed"]  # nothing returns through the index
    assert out["raw"] == ("expired", False)


def test_deleted_revoked_or_newer_source_takes_its_archived_memory_out(tmp_path):
    async def scenario():
        out: dict = {}
        async with world(tmp_path, [f"Ответ {n}." for n in range(6)]) as env:
            rt, scope = env.runtime, env.world.scope
            lamp = await fact(env, "lamp", "Ника: мой талисман — бирюзовый фонарик.")
            await claim(rt, scope, "lamp", "По словам Ники, её талисман — бирюзовый фонарик.", lamp)
            await summary(rt, scope, "lamp", "По словам Ники, бирюзовый фонарик — талисман.", lamp)
            bird = await fact(env, "bird", "Ника: моя любимая птица — серая цапля.")
            await claim(rt, scope, "bird", "По словам Ники, её любимая птица — серая цапля.", bird)
            isle = await fact(env, "isle", "Ника: мой любимый остров — Сосновый.")
            await claim(rt, scope, "isle", "По словам Ники, её любимый остров — Сосновый.", isle)
            await noise(env)
            asked = ("Какой у меня талисман?", "Какая моя любимая птица?", "Какой любимый остров?")
            out["before"] = [await shown(env, f"b{n}", q) for n, q in enumerate(asked)]
            # deletion through the panel plan, the retention owner and the independent ledger
            env.source_id = lamp.source_id
            await power(env, "stop-for-delete", "stop")
            executed = await post(env, await plan(env, "delete"))
            assert executed.status_code == 200, executed.text
            await power(env, "start-after-delete", "start")
            out["ledger"] = [e.source_id for e in await env.ledger.entries(scope)]
            # transfer to the API revoked; a new version of the source with the same rights
            await memory(rt).set_rights(
                command(scope, "revoke-bird"),
                bird.source_id,
                bird.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            await memory(rt).set_rights(
                command(scope, "touch-isle"),
                isle.source_id,
                isle.version,
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
            )
            out["after"] = [await shown(env, f"a{n}", q) for n, q in enumerate(asked)]
            out["recall"] = await env.runtime.store.repository(Source, OPERATOR, scope).get(
                lamp.source_id, purpose="api_transfer"
            )
        return out

    out = asyncio.run(scenario())
    lamp, bird, isle = out["before"]
    assert "бирюзовый фонарик" in lamp and "серая цапля" in bird and "Сосновый" in isle
    assert len(out["ledger"]) == 1 and out["recall"] is None
    lamp, bird, isle = out["after"]
    assert "бирюзовый" not in lamp  # neither the claim nor the summary of the deleted source
    assert "цапля" not in bird  # no API transfer: not in the request
    assert "Сосновый" not in isle  # cites version 1 of a source now at version 2
