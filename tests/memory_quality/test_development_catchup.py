"""Public profile: stopped across quota renewal -> reflection -> bounded state -> next reply."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_agent_application.control.ports import Clock
from ai_agent_application.extraction.evidence import experience_groups
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Episode
from ai_agent_contracts.persona import CharacterState, Profile, StateChange
from ai_agent_contracts.states import ErrorCode
from quality_support import Model, prepared_host
from test_http import scope_params
from usage_support import command, report

from ai_agent_core.local_host.budget import configure_budget
from ai_agent_core.m3_composition.state import PRINCIPALS


class DevelopmentModel(Model):
    def __init__(self):
        super().__init__()
        self.queues["story-screen"] = []

    @staticmethod
    def role(body):
        if "Правила JSON:" in body["messages"][0]["content"]:
            return "story-screen"
        return Model.role(body)


def test_stopped_for_days_with_exhausted_budget_catches_up_once_and_develops(tmp_path, monkeypatch):
    async def scenario():
        virtual = SimpleNamespace(now=datetime.now(UTC), elapsed=0.0)
        clock = Clock(lambda: virtual.elapsed, lambda: virtual.now)
        monkeypatch.setattr("ai_agent_application.control.supervisor.Clock", lambda: clock)
        model = DevelopmentModel()
        host = await prepared_host(tmp_path, model, monkeypatch)
        # A small real configuration makes exhausting the previous day inexpensive.
        configure_budget(host.root, limit=11, period="utc_day")
        async with host.open():
            await host.start()
            for number, fact in enumerate(
                ("В башне три окна.", "У причала стоит лодка.", "На площади есть часы.")
            ):
                await host.say(f"event-{number}", fact, "Получила новый факт для заметки.")
            rt = host.runtime
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=50)
            assert len(episodes) == 3
            groups = experience_groups(episodes)
            assert len({groups[e.experience_source_ids[0]] for e in episodes}) == 3
            assert all(len(e.experience_source_ids) == 2 for e in episodes)
            assert max(len(e.sources) for e in episodes) > 2  # shared context retained for rights
            profile = await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], host.scope).get(
                host.scope.persona_id
            )
            bounds = profile.development_bounds.data["bounds"]
            assert sum(b["autonomous"] is True for b in bounds) == 6
            assert next(b for b in bounds if b["name"] == "display_name")["autonomous"] is False
            ledger = UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry, clock=clock.utc)
            for number in range(8):
                failed = replace(
                    report(
                        f"previous-day-memory-{number}",
                        status="failed",
                        role="extractor",
                        at=virtual.now,
                    ),
                    connection_id="conn-live",
                )
                await ledger.record_attempt(command(host.scope, f"spent-{number}"), failed)
            status = (
                await host.env.client.get(
                    "/api/v1/private-api-status", params=scope_params(host.scope)
                )
            ).json()
            assert status["attempts_remaining"] == 0 and status["memory_calls_used"] == 8
            with pytest.raises(ContractError) as refused:
                await rt.daily_memory.enqueue([e.id for e in episodes])
            assert refused.value.error.code is ErrorCode.RESOURCE_BUSY
            assert not await rt.daily_memory.cycles(host.scope)
        calls = model.count()
        virtual.now += timedelta(days=3)
        virtual.elapsed += 3 * 86400
        model.queues["extractor"].append(dict(proposals=[]))
        model.queues["consolidator"].append(
            dict(
                content=(
                    "Три подтверждённых обмена новыми сведениями: окна башни, "
                    "лодка у причала и часы на площади."
                )
            )
        )
        model.queues["reflection"].append(
            dict(
                changes=[
                    dict(
                        property="curiosity",
                        new_value=0.77,
                        interpretation="Новые сведения поддержали интерес к наблюдениям.",
                        alternative="Возможно, интерес вызван только темой этих разговоров.",
                        uncertainty=0.2,
                        expected_manifestation=(
                            "При новой детали предложить один уместный уточняющий вопрос."
                        ),
                        episode_ids=[e.id for e in episodes],
                    )
                ]
            )
        )
        model.queues["story-screen"].extend([dict(hard=[], soft=[], uncertain=False)] * 2)
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await rt.daily_memory.tick()
            assert model.count() == calls  # quota renewal never starts the agent
            status = (
                await host.env.client.get(
                    "/api/v1/private-api-status", params=scope_params(host.scope)
                )
            ).json()
            assert status["attempts_used"] == status["memory_calls_used"] == 0
            await host.start()
            await rt.daily_memory.tick()  # no manual process-memory command or midnight event
            for _ in range(100):
                cycles = await rt.daily_memory.cycles(host.scope)
                if cycles:
                    break
                await asyncio.sleep(0.05)
            assert len(cycles) == 1
            await host.finish(cycles[0].operation_id)
            state = await rt.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], host.scope
            ).get("state:" + host.scope.persona_id)
            assert state.values.data["values"]["curiosity"] == 0.77
            changes = await rt.store.repository(
                StateChange, PRINCIPALS[Owner.PERSONA], host.scope
            ).list(limit=50)
            assert len(changes) == 1 and changes[0].status.value == "APPLIED"
            system = model.last("reflection")["messages"][0]["content"]
            assert "Любознательность" in system and '"min_independent_episodes": 3' in system
            assert '"max_step_per_window": 0.05' in system
            await host.say(
                "after-growth",
                "Что ты думаешь о следующей находке?",
                "Будет основание — уточню деталь.",
            )
            assert "curiosity: 0.77" in model.last("character")["messages"][0]["content"]
            assert model.count("reflection") == 1
            # Three calls left are insufficient for a new changing-character batch.
            latest = await host.get("episodes")
            fresh = next(e for e in latest if e["id"] not in {old.id for old in episodes})
            before = model.count()
            with pytest.raises(ContractError) as no_room:
                await rt.daily_memory.enqueue([fresh["id"]])
            assert no_room.value.error.code is ErrorCode.RESOURCE_BUSY
            assert model.count() == before
        calls = model.count()
        async with host.open():
            await host.start()
            await host.runtime.daily_memory.tick()
            assert model.count() == calls and model.count("reflection") == 1
            assert len(await host.runtime.daily_memory.cycles(host.scope)) == 1

    asyncio.run(scenario())
