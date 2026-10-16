"""Every skip is free; dialogue first; budget, screen refusal, character refusal, STOP, restart."""

import asyncio
from dataclasses import replace

from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.states import OperationState
from private_gm_support import (
    BOUNDS,
    CLEAR,
    DRAFT,
    LINE,
    GmModel,
    due,
    gm_host,
    offers,
    slot_operation,
    unpause,
)
from test_http import post

from ai_agent_core.m3_composition.state import PRINCIPALS

GM_ROLES = ("game-master", "story-screen")


def gm_calls(model):
    return sum(model.count(role) for role in GM_ROLES)


def test_skip_reasons_make_no_call_and_dialogue_and_reserves_come_first(tmp_path, monkeypatch):
    async def scenario():
        model = GmModel()
        host = await gm_host(
            tmp_path, model, monkeypatch, enabled=True, line_direction=LINE, line_boundaries=BOUNDS
        )
        out = []
        async with host.open():
            rt = host.runtime
            out.append(await due(host))  # STOPPED after open
            await host.start()
            out.append(await due(host))
            await unpause(host)
            out.append(await due(host))
            await host.say("m1", "Сегодня я разбирала почту редакции.", "Интересно.")
            line, rt.gm_line = rt.gm_line, None
            out.append(await due(host))
            rt.gm_line = line
            # A reply in progress has priority over the GM.
            gate = model.hold["character"] = asyncio.Event()
            model.queues["character"].append(dict(reply="Позже."))
            run = rt.supervisor.snapshot
            made = await post(
                host.env,
                dict(
                    type="dialogue.message",
                    request_id="busy",
                    target=run.session_id,
                    scope=run.scope.model_dump(mode="json"),
                    arguments={"text": "Ты здесь?"},
                ),
            )
            for _ in range(200):
                if getattr(model, "waiting", False):
                    break
                await asyncio.sleep(0.02)
            out.append(await due(host))
            gate.set()
            del model.hold["character"]
            await host.finish(made.json()["operation_id"])
            # The memory reserve and the dialogue floor are never spent by the GM.
            budget = rt.private_api_budget
            rt.private_api_budget = replace(
                budget, max_attempts=22, memory_processing=True, memory_daily_calls=8
            )
            attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(
                host.scope
            )
            now = rt.supervisor.clock.utc()
            room = rt.private_api_budget.gm_room(attempts, now)
            out.append(await due(host))
            rt.private_api_budget = replace(budget, gm_daily_calls=2)  # a new line needs 3
            out.append(await due(host))
            rt.private_api_budget = budget
            return out, room, gm_calls(model)

    reasons, room, calls = asyncio.run(scenario())
    assert reasons == [
        "RUN_NOT_AUTONOMOUS",
        "GM_PAUSED",
        "NO_EXPERIENCE",
        "NO_LINE",
        "FOREGROUND_BUSY",
        "BUDGET",
        "BUDGET",
    ]
    assert room == 2 and calls == 0  # 22 - 2 used - 8 reserved - 10 floor


def test_screen_refusal_stop_mid_call_restart_and_character_refusal(tmp_path, monkeypatch):
    async def scenario():
        model = GmModel()
        host = await gm_host(
            tmp_path,
            model,
            monkeypatch,
            enabled=True,
            line_direction=LINE,
            line_boundaries=BOUNDS,
            daily_offers=3,
            daily_calls=8,
        )
        out: dict = {}
        async with host.open():
            rt = host.runtime
            await host.start()
            await unpause(host)
            await host.say("m1", "Я сегодня нашла лишнюю запятую.", "Любопытно.")
            # 1. an unsure story screen closes the offer (and sets no cooldown)
            model.queues["story-screen"] += [CLEAR, dict(hard=[], soft=[], uncertain=True)]
            model.queues["game-master"].append(DRAFT)
            assert await due(host) == "SUBMITTED"
            await host.finish(slot_operation(host, 1))
            [blocked] = await offers(host)
            await host.say("m2", "А ещё в заголовке была опечатка.", "Записала.")
            out["blocked"] = (
                blocked.status.value,
                "<offer" not in model.last("character")["messages"][-1]["content"],
            )
            # 2. STOP while the GM model call is in flight: nothing is applied
            gate = model.hold["game-master"] = asyncio.Event()
            model.waiting = False
            model.queues["game-master"].append(dict(DRAFT, topic_key="headline-typo"))
            model.queues["story-screen"].append(CLEAR)
            out["second"] = await due(host)
            for _ in range(300):
                if model.waiting:
                    break
                await asyncio.sleep(0.02)
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                "/api/v1/agents/primary/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="stop-gm",
                    target="primary",
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200
            gate.set()
            del model.hold["game-master"]
            job = await host.finish(slot_operation(host, 2), succeeded=False)
            out["stopped_job"] = job.status
            out["after_stop"] = sorted(o.status.value for o in await offers(host))
            out["calls"] = gm_calls(model)
            model.queues["story-screen"].clear()
        async with host.open():  # restart: STOPPED, nothing repeated or billed again
            rt = host.runtime
            out["restart"] = (rt.supervisor.snapshot.state.value, await due(host))
            await host.start()
            await unpause(host)
            rt.autonomous_gm.next_check = 0.0
            out["first_tick"] = await rt.autonomous_gm.tick()
            out["calls_after_restart"] = gm_calls(model)
            # 3. a clear offer, which the character declines; the talk goes on
            await host.say("m3", "Сегодня в редакции тихо.", "Хорошо.")
            model.queues["story-screen"].append(CLEAR)
            model.queues["game-master"].append(dict(DRAFT, topic_key="quiet-office"))
            assert await due(host) == "SUBMITTED"
            await host.finish(slot_operation(host, 3))
            offered = next(o for o in await offers(host) if o.status.value == "OFFERED")
            model.queues["character"].append(
                dict(
                    reply="Пожалуй, не сейчас.",
                    offer=dict(
                        opportunity_id=offered.id, version=offered.version, decision="decline"
                    ),
                )
            )
            await host.say("m4", "Что думаешь?", "unused")
            model.queues["character"].pop()
            await host.say("m5", "Хорошо, тогда о погоде.", "Пасмурно.")
            out["declined"] = next(o for o in await offers(host) if o.id == offered.id).status.value
            out["daily"] = await due(host)
        return out

    out = asyncio.run(scenario())
    assert out["blocked"] == ("BLOCKED", True)
    assert out["second"] == "SUBMITTED"
    assert out["stopped_job"] != OperationState.SUCCEEDED
    assert out["after_stop"] == ["BLOCKED"]  # the in-flight result was never applied
    assert out["restart"] == ("STOPPED", "RUN_NOT_AUTONOMOUS")
    assert out["first_tick"] == "INTERVAL"  # no burst after a restart
    assert out["calls_after_restart"] == out["calls"]  # nothing repeated or billed again
    assert out["declined"] == "DECLINED"
    assert out["daily"] == "LINE_COOLDOWN"  # after an offer the line rests (no paid call)
