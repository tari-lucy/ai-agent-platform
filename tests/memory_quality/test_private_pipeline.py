"""One reproducible path: confirmed talk → out of the window → memory cycle → restart → new talk.

Production-built host, the public profile, real HTTP owners and the real live adapter against a
synthetic scripted model. What the model SEES is asserted from the request body it received.
A scripted answer proves the route and the rules, not the quality of free text.
"""

import asyncio
import logging

from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Claim, MemoryCycle
from ai_agent_contracts.persona import CharacterState
from quality_support import Model, prepared_host, view
from test_http import scope_params

from ai_agent_core.m3_composition.state import PRINCIPALS

PREFERENCE = "Мне нравятся синие зонтики"
AGREEMENT = "договоримся встретиться у маяка в пятницу"
FIRST = (
    f"Меня зовут Ника, я из вымышленного города Лунный Брод. {PREFERENCE}, а в пятницу {AGREEMENT}."
)
OWN_WRONG = "Запомнила: красные зонтики и маяк в пятницу."
FILLER = [
    ("Расскажи, как выглядит порт утром.", "Порт утром тихий, чайки и мокрые канаты."),
    ("А что пишут в газете о ярмарке?", "Пишут, что ярмарка откроется после дождя."),
    ("Какой чай ты бы выбрала для дороги?", "Чёрный с бергамотом, чтобы не заснуть в пути."),
    ("Мне нужно сделать паузу на минуту.", "Конечно, я подожду тебя здесь."),
]
LONG = " ".join(f"Обсуждали вымышленную тему номер {i}: порт, чай, газета." for i in range(24))
SUMMARY = (
    LONG + " По словам Ники, ей нравятся синие зонтики. По словам Ники, договорились встретиться"
    " у маяка в пятницу."
)


def test_detail_beyond_the_window_reaches_the_next_reply_after_restart(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)

    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            first = await host.say("m1", FIRST, OWN_WRONG)
            for index, (text, reply) in enumerate(FILLER, start=2):
                await host.say(f"m{index}", text, reply)
            episodes = [e["id"] for e in await host.get("episodes")]
            assert len(episodes) == 5
            sources = await host.sources()
            person, own = sources[FIRST], sources[OWN_WRONG]
            model.queues["extractor"].append(
                dict(
                    proposals=[
                        dict(
                            subject_id="interlocutor",
                            claim="Собеседнице нравятся синие зонтики.",
                            proposed_type="observed",  # asks for more than a person's word proves
                            confidence=0.95,
                            evidence=[dict(source_id=person, quote=PREFERENCE)],
                            contradicts=[],
                        ),
                        dict(
                            subject_id="interlocutor",
                            claim="Собеседница договорилась встретиться у маяка в пятницу.",
                            proposed_type="reported",
                            confidence=0.8,
                            evidence=[dict(source_id=person, quote=AGREEMENT)],
                            contradicts=[],
                        ),
                        dict(
                            subject_id="interlocutor",
                            claim="Собеседница любит красные зонтики.",
                            proposed_type="reported",
                            confidence=0.9,
                            evidence=[dict(source_id=own, quote="красные зонтики")],
                            contradicts=[],
                        ),
                        dict(  # an invented shared memory: no source says it
                            subject_id="interlocutor",
                            claim="Они вместе ездили на море.",
                            proposed_type="reported",
                            confidence=0.9,
                            evidence=[dict(source_id=person, quote="ездили на море")],
                            contradicts=[],
                        ),
                    ]
                )
            )
            model.queues["consolidator"].append(dict(content=SUMMARY))
            model.queues["reflection"].append(
                dict(keep_state_reason="Одного разговора мало для изменения характера.", changes=[])
            )
            state_before = await host.runtime.store.repository(
                CharacterState, PRINCIPALS[Owner.PERSONA], host.scope
            ).list()
            cycle = await host.process("cycle-1", episodes)
            assert cycle.status_code == 200, cycle.text
            await host.finish(cycle.json()["operation_id"])
            out["paid_calls"] = (model.count("extractor"), model.count("consolidator"))
            claims = await host.runtime.store.repository(
                Claim, PRINCIPALS[Owner.MEMORY], host.scope
            ).list()
            out["claims"] = {c.content: (c.knowledge_type, c.confidence) for c in claims}
            rows = await host.runtime.store.repository(
                MemoryCycle, PRINCIPALS[Owner.MEMORY], host.scope
            ).list()
            out["cycle"] = [(r.stage, r.status) for r in rows]
            linked = host.runtime.dialogue_index._db.execute(
                "SELECT COUNT(DISTINCT claim_id) FROM claim_sources"
            ).fetchone()[0]
            out["indexed_claims"] = linked
            out["state_same"] = (
                state_before
                == await host.runtime.store.repository(
                    CharacterState, PRINCIPALS[Owner.PERSONA], host.scope
                ).list()
            )
            # the same batch again, even under another request id, is not paid for twice
            again = await host.process("cycle-1-again", episodes)
            out["replay"] = again.json()["operation_id"] == cycle.json()["operation_id"]
            out["calls_after_cycle"] = model.count()
            out["first_id"] = first.id
        async with host.open():  # restart: nothing runs by itself
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            out["calls_after_restart"] = model.count()
            await host.start()
            host.runtime.daily_memory.next_check = 0
            await host.runtime.daily_memory.tick()
            out["cycles_after_tick"] = len(await host.runtime.daily_memory.cycles(host.scope))
            await host.say(
                "after-restart",
                "Ника снова здесь. Напомни, какие зонтики мне нравятся и о чём мы договорились?",
                "Синие зонтики, и встреча у маяка в пятницу.",
            )
            out["status"] = (
                await host.env.client.get(
                    "/api/v1/private-api-status", params=scope_params(host.scope)
                )
            ).json()
            out["view"] = view(model.last("character"))
            out["system"] = model.last("character")["messages"][0]["content"]
            out["extractor_system"] = model.last("extractor")["messages"][0]["content"]
            out["consolidator_system"] = model.last("consolidator")["messages"][0]["content"]
        return out

    out = asyncio.run(scenario())

    # 1. accepted / rejected are distinguishable, and the rules were not loosened for a count
    assert out["paid_calls"] == (1, 1) and out["cycle"] == [("done", "completed")]
    assert out["indexed_claims"] == 3  # recall's claim links are written by the memory job
    claims = out["claims"]
    assert len(claims) == 3, claims
    reported = {c: v for c, v in claims.items() if v[0] == "reported"}
    assert len(reported) == 2 and all(conf <= 0.6 for _, conf in reported.values())
    own_claim = next(c for c, v in claims.items() if "красные" in c)
    assert claims[own_claim][0] == "interpretation" and claims[own_claim][1] <= 0.4
    assert not any("море" in c for c in claims) and not any(
        t == "observed" for t, _ in claims.values()
    )
    line = next(r.message for r in caplog.records if r.message.startswith("Extraction outcome="))
    assert "offered=10 proposed=4 accepted=3" in line and "QUOTE_NOT_IN_SOURCE:1" in line
    assert "зонтик" not in line and "маяк" not in line
    # 2. replay and restart do not repeat a paid stage; reflection kept the character
    assert out["replay"] and out["calls_after_cycle"] == 5 + 3
    assert out["calls_after_restart"] == out["calls_after_cycle"]
    assert out["cycles_after_tick"] == 1 and out["state_same"]
    status = out["status"]
    assert status["memory_calls_used"] == 3 and status["attempts_used"] == 5 + 3 + 1
    assert status["dialogue_remaining"] == 300 - 8 - 6  # the memory reserve is inside the limit
    # 3. the next reply sees the old detail through memory, not through the 8-turn window
    seen = out["view"]
    texts = " ".join(text for _, text in seen["turns"])
    assert FIRST not in texts and "Лунный Брод" not in texts and len(seen["turns"]) == 8
    labelled = {m["by"]: m for m in seen["memory"]}
    kinds = [m["by"] for m in seen["memory"]]
    assert "сводка-интерпретация" in labelled and kinds.count("утверждение-reported") == 2
    assert labelled["сводка-интерпретация"]["partial"]
    assert "синие зонтики" in labelled["сводка-интерпретация"]["text"]
    assert "у маяка в пятницу" in labelled["сводка-интерпретация"]["text"]
    # the character's own paraphrase is outranked and, if ever shown, never as a person's word
    assert all(
        "красные" not in m["text"] or m["by"] == "утверждение-interpretation"
        for m in seen["memory"]
    )
    shown = " ".join(m["text"] for m in seen["memory"])
    assert "море" not in shown and "ездили" not in shown  # no invented shared history
    assert all(m["at"] for m in seen["memory"])
    # long-term memory keeps its 3 fragments; the separate grounded biography adds the kept state
    biography = [m for m in seen["memory"] if m["by"] == "вывод-рефлексии-оставить"]
    assert len(seen["memory"]) - len(biography) <= 3
    assert [m["text"] for m in biography] == ["Одного разговора мало для изменения характера."]
    # the closed wire protocol states the output limits and pays for the schema once
    extractor = out["extractor_system"]
    assert extractor.count('"$defs"') == 1 and "Схема приведена выше." in extractor
    assert "не более 5 утверждений" in extractor and "кратчайший дословный фрагмент" in extractor
    assert '"content"' in out["consolidator_system"].split("## Протокол приложения")[1]
    # 4. what was kept out of this request is visible to the operator as counts
    context = next(r.message for r in caplog.records if r.message.startswith("Private memory"))
    assert "shown=" in context and "stale_source=0" in context
    assert "Опечатка" in out["system"] or len(out["system"]) > 1000  # the public profile is used
