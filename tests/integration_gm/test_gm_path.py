"""Experience -> GM offer (line + draft + story screen) -> next reply sees only the offer ->
the character's explicit decision by id/version -> only a later confirmed exchange is experience."""

import asyncio
import logging
from types import SimpleNamespace

from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.memory import Episode
from private_gm_support import (
    BOUNDS,
    CLEAR,
    DRAFT,
    LINE,
    GmModel,
    due,
    gm_host,
    lines,
    offers,
    slot_operation,
    unpause,
)

from ai_agent_core.m3_composition.state import PRINCIPALS


def test_confirmed_experience_becomes_an_offer_a_decision_and_then_experience(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)

    async def scenario():
        model = GmModel()
        host = await gm_host(
            tmp_path, model, monkeypatch, enabled=True, line_direction=LINE, line_boundaries=BOUNDS
        )
        out: dict = {}
        async with host.open():
            rt = host.runtime
            await host.start()
            out["paused_at_start"] = await due(host)  # every new run starts with GM paused
            await unpause(host)
            out["no_experience"] = await due(host)
            await host.say(
                "m1", "Я сегодня нашла в письме лишнюю запятую.", "Запятые — мой профиль."
            )
            # interval not yet elapsed after the first eligible moment: no call
            rt.autonomous_gm.next_check, rt.autonomous_gm.next_active = 0.0, None
            out["interval"] = await rt.autonomous_gm.tick()
            model.queues["story-screen"] += [CLEAR, CLEAR]
            model.queues["game-master"].append(DRAFT)
            calls_before = model.count()
            out["submitted"] = await due(host)
            await host.finish(slot_operation(host))
            out["gm_calls"] = (model.count("game-master"), model.count("story-screen"))
            out["calls_added"] = model.count() - calls_before
            [offer] = await offers(host)
            # Native OpenCode receives the same checked optional offer through MCP.
            # Stub only the unrelated persona builder; GM and its source checks are real.
            from ai_agent_core.m3_composition import workplace_experience
            from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

            class Brief:
                async def build(self, scope, topic):
                    return SimpleNamespace(text="synthetic persona")

            with monkeypatch.context() as patch:
                patch.setattr(workplace_experience, "persona", lambda _: Brief())
                native = PlatformMcp(rt, lambda: rt.supervisor.snapshot.control_epoch)
                result = await native.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "persona_brief", "arguments": {}},
                    }
                )
                native_text = result["result"]["content"][0]["text"]
                out["native_seen"] = (
                    f'<offer id="{offer.id}"' in native_text,
                    LINE not in native_text,
                    DRAFT["topic_key"] not in native_text,
                )
            out["offer"] = (offer.status.value, offer.family_id)
            out["line"] = [(a.status, a.direction == LINE) for a in await lines(host)]
            attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(
                host.scope
            )
            now = rt.supervisor.clock.utc()
            budget = rt.private_api_budget
            out["usage"] = (budget.gm_used(attempts, now), budget.memory_used(attempts, now))
            # the same moment again: one pending offer, no second job
            out["pending"] = await due(host)

            model.queues["character"].append(
                dict(
                    reply="Хорошая мысль — спрошу.",
                    question=None,
                    offer=dict(opportunity_id=offer.id, version=offer.version, decision="accept"),
                )
            )
            await host.say("m2", "О чём ещё поговорим?", "unused")
            model.queues["character"].pop()  # say() queues its own reply; ours was used first
            seen = model.last("character")["messages"][-1]["content"]
            out["seen"] = (
                f'<offer id="{offer.id}"' in seen,
                LINE in seen,
                DRAFT["topic_key"] in seen,
                "небольшое размышление" in seen or "сила:" in seen,  # family title / rolls
            )
            [after] = await offers(host)
            out["accepted"] = (after.status.value, (after.choice or "").startswith("accepted-in:"))
            episodes_before = len(
                await rt.store.repository(Episode, PRINCIPALS[Owner.MEMORY], host.scope).list(
                    limit=200
                )
            )
            await host.say("m3", "Да, однажды пропала целая запятая!", "Записываю.")
            [done] = await offers(host)
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=200)
            out["resolved"] = (
                done.status.value,
                any(e.operation_id == offer.id for e in episodes),
                len(episodes) - episodes_before,
            )
        return out

    out = asyncio.run(scenario())
    assert out["paused_at_start"] == "GM_PAUSED" and out["no_experience"] == "NO_EXPERIENCE"
    assert out["interval"] == "INTERVAL"
    assert out["submitted"] == "SUBMITTED"
    assert out["gm_calls"] == (1, 2) and out["calls_added"] == 3  # line screen, draft, offer screen
    assert out["offer"] == ("OFFERED", "private-conversation")
    assert out["native_seen"] == (True, True, True)
    assert out["line"] == [("open", True)]
    assert out["usage"] == (3, 0)  # the shared screen is not charged to memory
    assert out["pending"] == "OFFER_PENDING"
    assert out["seen"] == (True, False, False, False)  # the offer, never the plan behind it
    assert out["accepted"] == ("ACCEPTED", True)
    assert out["resolved"][:2] == ("RESOLVED", True)
    log = caplog.text
    assert "GM job: offered=true reason=OK" in log
    assert "Опечатка расследует" not in log and "запятые" not in log.lower()
