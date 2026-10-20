"""After a restart the reply sees why the character changed; a deleted ground removes it.

Production-built host, the public profile, real HTTP owners and the live adapter over a synthetic
scripted transport (memory roles included). What the model SAW is asserted from its request; a
scripted answer proves the route and the rules, not the quality of free text.
"""

import asyncio
import logging

from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.persona import StateChange
from quality_support import prepared_host, view
from test_development_catchup import DevelopmentModel

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command

FACTS = ("В башне три окна.", "У причала стоит лодка.", "На площади есть часы.")
REASON = "Новые сведения поддержали интерес к наблюдениям."


def biography(body):
    return [m for m in view(body)["memory"] if m["by"] in BIO]


BIO = {"изменение-характера", "откат-изменения", "событие", "вывод-рефлексии-оставить"}


def test_a_restarted_reply_sees_the_grounded_reason_of_a_change(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        model = DevelopmentModel()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            for number, fact in enumerate(FACTS):
                await host.say(f"event-{number}", fact, "Получила новый факт для заметки.")
            out["empty"] = biography(model.last("character"))  # no history yet: nothing invented
            rt = host.runtime
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=50)
            model.queues["extractor"].append(dict(proposals=[]))
            model.queues["consolidator"].append(dict(content="Три обмена новыми сведениями."))
            model.queues["reflection"].append(
                dict(
                    changes=[
                        dict(
                            property="curiosity",
                            new_value=0.77,
                            interpretation=REASON,
                            alternative="Возможно, интерес вызван только темой разговоров.",
                            uncertainty=0.2,
                            expected_manifestation="При новой детали задать уточняющий вопрос.",
                            episode_ids=[e.id for e in episodes],
                        )
                    ]
                )
            )
            model.queues["story-screen"].extend([dict(hard=[], soft=[], uncertain=False)] * 2)
            cycle = await host.process("cycle-1", [e.id for e in episodes])
            assert cycle.status_code == 200, cycle.text
            await host.finish(cycle.json()["operation_id"])
            change = (
                await rt.store.repository(StateChange, PRINCIPALS[Owner.PERSONA], host.scope).list()
            )[0]
            out["status"] = change.status.value
        calls = model.count("character")
        async with host.open():  # restart: the reason survives in authoritative records
            await host.start()
            rt = host.runtime
            await host.say("after", "Почему ты стала любопытнее?", "Новые сведения.")
            out["calls"] = model.count("character") - calls
            out["after"] = biography(model.last("character"))
            out["system"] = model.last("character")["messages"][0]["content"]
            # one of the grounds is deleted: the reason disappears, the state value stays
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            first = {s.id for s in sources if s.content == FACTS[0]}
            ground = next(ref for ref in change.evidence if ref.source_id in first)
            source = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).get(
                ground.source_id
            )
            await memory(rt).delete_source(
                command(source.scope, "delete-ground"), source.id, source.version
            )
            caplog.clear()
            await host.say("deleted", "Почему ты стала любопытнее?", "Не помню основания.")
            out["deleted"] = biography(model.last("character"))
            out["log"] = next(
                r.message for r in caplog.records if r.message.startswith("Private biography")
            )
        return out

    out = asyncio.run(scenario())
    assert out["empty"] == []
    assert out["status"] == "APPLIED"
    after = out["after"]
    assert [m["by"] for m in after] == ["изменение-характера"]
    # the change, its size and the reflection's own uncertainty, never a settled fact
    assert after[0]["text"] == f"curiosity: 0.75 → 0.77 (уверенность 0.8). {REASON}"
    assert after[0]["at"]  # dated
    assert out["calls"] == 1  # one model call for the reply: no extra call for the biography
    assert "curiosity: 0.77" in out["system"]
    # storage already hides a change whose ground was deleted; nothing of it reaches the model
    assert out["deleted"] == [] and "shown=0" in out["log"]
