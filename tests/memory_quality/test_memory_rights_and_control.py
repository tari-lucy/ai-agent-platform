"""Derived memory follows source rights and control state; a dispute is shown, not erased."""

import asyncio
from datetime import timedelta

import pytest
from ai_agent_adapters.llm.mock import Pause
from ai_agent_application.memory import ClaimDraft, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.memory import Claim
from ai_agent_contracts.persona import CharacterState, StateChange
from ai_agent_contracts.states import ErrorCode, Provenance
from dev_support import answer, idea
from integration_support import prepared
from quality_support import view
from test_history import prompt, send
from test_http import finish_job, post, start
from test_memory_cycles import seed

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.service import PrivateAPIBudget
from ai_agent_core.m3_composition.state import PRINCIPALS, command


async def said(env, name, text):
    """A stored person statement, as the dialogue intake would register it."""
    scope = env.world.scope
    env.world.clock.now += timedelta(milliseconds=10)
    made = await memory(env.runtime).register_source(
        command(scope, "src-" + name),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="operator-message",
            adapter_id="dialogue",
            adapter_version="1",
            occurred_at=env.runtime.supervisor.clock.utc(),
            content=text,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            external_id="ext-" + name,
        ),
    )
    return SourceRef(
        source_id=made.source_id, version=made.version, scope=scope, availability="available"
    )


async def claim(env, name, text, ref, *, conflicts=()):
    env.world.clock.now += timedelta(milliseconds=10)
    await memory(env.runtime).record_claim(
        command(env.world.scope, "clm-" + name),
        ClaimDraft(
            claim_id="claim-" + name,
            subject_id="interlocutor",
            content=text,
            knowledge_type="reported",
            sources=(ref,),
            confidence=0.6,
            conflicts_with=tuple(conflicts),
        ),
    )


def allow_memory(env):
    env.runtime.private_api_budget = PrivateAPIBudget("conn-a", 300, 24000, "utc_day", True, 8)


def fragments(env):
    messages = [{"content": m.content} for m in env.world.provider.calls[-1].messages]
    return view({"messages": messages})["memory"]


def test_a_contradiction_is_shown_beside_the_old_claim_and_rights_close_each_side(tmp_path):
    async def scenario():
        replies = [answer(reply=f"Ответ {i}.") for i in range(3)]
        async with prepared(tmp_path, replies) as env:
            await start(env)
            allow_memory(env)
            old = await said(env, "old", "Ника: встреча у маяка назначена на пятницу.")
            new = await said(env, "new", "Ника: встречу у маяка перенесли на субботу.")
            await claim(env, "old", "По словам Ники: встреча у маяка в пятницу.", old)
            await claim(
                env,
                "new",
                "По словам Ники: встречу у маяка перенесли на субботу.",
                new,
                conflicts=["claim-old"],
            )
            out = {}
            await send(env, "q1", "Когда встреча у маяка?")
            out["both"] = fragments(env)
            stored = await env.runtime.store.repository(
                Claim, PRINCIPALS[Owner.MEMORY], env.world.scope
            ).list()
            out["stored"] = sorted(c.id for c in stored)
            await memory(env.runtime).set_rights(
                command(env.world.scope, "revoke-new"),
                new.source_id,
                new.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            await send(env, "q2", "Когда встреча у маяка?")
            out["revoked"] = fragments(env)
            await memory(env.runtime).delete_source(
                command(env.world.scope, "delete-old"), old.source_id, old.version
            )
            await send(env, "q3", "Когда встреча у маяка?")
            out["deleted"] = fragments(env)
            return out

    out = asyncio.run(scenario())
    assert out["stored"] == ["claim-new", "claim-old"]  # nothing was overwritten
    both = out["both"]
    assert len(both) == 2 and all(m["disputed"] and m["at"] for m in both)
    assert "субботу" in both[0]["text"] and "пятницу" in both[1]["text"]  # newest first
    assert [m["text"] for m in out["revoked"]] == [both[1]["text"]]
    assert not out["revoked"][0]["disputed"]  # no trace of the withdrawn side
    assert out["deleted"] == []


def test_a_reply_that_used_derived_memory_is_not_saved_after_its_source_is_revoked(tmp_path):
    async def scenario():
        slow = answer(reply="Поздний ответ.")
        slow.insert(1, Pause(2.0))  # wide enough to revoke while generating, even under load
        async with prepared(tmp_path, [slow]) as env:
            await start(env)
            allow_memory(env)
            ref = await said(env, "base", "Ника: встреча у маяка в пятницу.")
            await claim(env, "base", "По словам Ники: встреча у маяка в пятницу.", ref)
            run = env.runtime.supervisor.snapshot
            result = await post(
                env,
                dict(
                    type="dialogue.message",
                    request_id="late",
                    target=run.session_id,
                    scope=run.scope.model_dump(mode="json"),
                    arguments={"text": "Когда встреча у маяка?"},
                ),
            )
            assert result.status_code == 200, result.text
            # The job starts on a runtime tick; under load that can take seconds. Wait for the
            # model call itself (the reply is paused in the middle), not for a fixed time.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while not env.world.provider.calls and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert env.world.provider.calls, "the reply job never reached the model"
            assert 'by="утверждение-reported"' in prompt(env)
            await memory(env.runtime).set_rights(
                command(env.world.scope, "revoke-while-generating"),
                ref.source_id,
                ref.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            await finish_job(env, result.json()["operation_id"], succeeded=False)
            rows = await env.runtime.store.repository(
                Utterance, PRINCIPALS[Owner.GATEWAY], run.scope
            ).list()
            return [r.content for r in rows]

    assert "Поздний ответ." not in asyncio.run(scenario())


@pytest.mark.parametrize("close", ["pause", "stop"])
def test_pause_and_stop_each_hold_the_cycle_without_a_paid_call(tmp_path, close):
    async def scenario():
        async with prepared(tmp_path, [answer(reply="Вымышленный ответ.")]) as env:
            episode = await seed(env)
            rt = env.runtime
            calls = len(env.world.provider.calls)
            run = rt.supervisor.snapshot
            if close == "pause":
                held = await post(
                    env,
                    dict(
                        type="agent.pause",
                        request_id="pause-cycle",
                        target="primary",
                        scope=run.scope.model_dump(mode="json"),
                        expected_version=run.version,
                        arguments={},
                    ),
                )
                assert held.status_code == 200
            else:
                held = await env.client.post(
                    "/api/v1/agents/primary/stop",
                    headers=env.headers,
                    json=dict(
                        type="agent.stop",
                        request_id="stop-cycle",
                        target="primary",
                        scope=run.scope.model_dump(mode="json"),
                    ),
                )
                assert held.status_code == 200
            with pytest.raises(ContractError) as refused:
                await rt.daily_memory.enqueue([episode])
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            return (
                refused.value.error.code,
                calls,
                len(env.world.provider.calls),
                await rt.daily_memory.cycles(rt.supervisor.snapshot.scope),
            )

    code, before, after, cycles = asyncio.run(scenario())
    assert code is ErrorCode.STALE_CONTEXT and before == after and cycles == []


def test_reflection_proposal_outside_the_bounded_policy_changes_nothing(tmp_path):
    async def scenario():
        async with prepared(tmp_path, [answer(reply="Вымышленный ответ.")]) as env:
            episode = await seed(env)
            rt = env.runtime
            env.world.provider.scripts[-1] = answer(
                keep_state_reason="",
                changes=[idea("curiosity", 5.0, episodes=(episode,))],  # far outside 0..1
            )
            state = await rt.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], rt.supervisor.snapshot.scope
            ).list()
            result = await rt.daily_memory.enqueue([episode])
            for _ in range(1200):
                stored = await rt.jobs.store.load(result.operation_id, rt.supervisor.snapshot.scope)
                if stored[0].status.value in (
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "OUTCOME_UNKNOWN",
                ):
                    break
                await asyncio.sleep(0.05)
            finished = stored[0]
            after = await rt.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], rt.supervisor.snapshot.scope
            ).list()
            changes = await rt.store.repository(
                StateChange, PRINCIPALS[Owner.PERSONA], rt.supervisor.snapshot.scope
            ).list()
            return state == after, [c.status.value for c in changes], finished.status.value

    same, statuses, job = asyncio.run(scenario())
    assert same and statuses == ["REJECTED"] and job == "SUCCEEDED"
