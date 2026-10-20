"""Which grounded records the biography shows: rollbacks undo, closed grounds and routine stay out.

Real owners/HTTP on a temporary SQLite; records are written through their owners' transactions.
What the scripted model SAW is read from its request.
"""

import asyncio
from datetime import timedelta

from ai_agent_application.memory import SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Episode
from ai_agent_contracts.persona import ReflectionCheckpoint, StateChange
from ai_agent_contracts.states import ChangeState, Provenance
from dev_support import answer
from integration_support import prepared
from quality_support import view
from test_history import send
from test_http import start
from test_memory_rights_and_control import allow_memory

from ai_agent_core.m3_composition import biography
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command, extension, put

BIO = {"изменение-характера", "откат-изменения", "событие", "вывод-рефлексии-оставить"}


async def source(env, name, text, *, transfer=True):
    env.world.clock.now += timedelta(milliseconds=10)
    rt, scope = env.runtime, env.world.scope
    made = await memory(rt).register_source(
        command(scope, "src-" + name),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="fixture",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=rt.store.clock(),
            content=text,
            read_allowed=True,
            api_transfer_allowed=transfer,
            public_disclosure_allowed=False,
        ),
    )
    return SourceRef(source_id=made.source_id, version=1, scope=scope, availability="available")


async def change(env, name, before, after, text, refs, *, kind="change", revision_of=None):
    env.world.clock.now += timedelta(milliseconds=10)
    rt, scope = env.runtime, env.world.scope
    now = rt.store.clock()
    record = StateChange.model_validate(
        dict(
            id="change-" + name,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            proposal_id="proposal-" + name,
            owner="persona",
            base_version=1,
            patch=extension(
                rt.registry,
                "development.patch",
                {"property": "curiosity", "before": before, "after": after, "kind": kind},
            ),
            evidence=refs,
            interpretation=text,
            uncertainty=0.4,
            expected_manifestation="Уточняющий вопрос.",
            status=ChangeState.APPLIED,
            new_version=2,
            revision_of=revision_of,
            operator_reason="Оператор отменил изменение." if kind == "rollback" else None,
        ),
        context={"extensions": rt.registry},
    )
    await put(rt.store, rt.registry, Owner.PERSONA, scope, "w-" + name, record, None, rt.readers)


async def episode(env, name, choice, outcome, refs):
    env.world.clock.now += timedelta(milliseconds=10)
    rt, scope = env.runtime, env.world.scope
    record = Episode(
        id="episode-" + name,
        scope=scope,
        created_at=rt.store.clock(),
        sources=refs,
        participant_ids=[],
        choice=choice,
        actual_outcome=outcome,
        operation_id="op-" + name,
        provenance="observation" if scope.memory_mode == "work" else "simulation",
        outcome_confirmed=True,
    )
    await put(rt.store, rt.registry, Owner.MEMORY, scope, "w-" + name, record, None, rt.readers)


async def kept(env, refs):
    env.world.clock.now += timedelta(milliseconds=10)
    rt, scope = env.runtime, env.world.scope
    now = rt.store.clock()
    record = ReflectionCheckpoint.model_validate(
        dict(
            id="reflection-kept",
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            trigger_id="cycle-kept",
            profile_version=1,
            state_version=1,
            sources=refs,
            response=extension(
                rt.registry,
                "development.reflection-result",
                {"keep_state_reason": "Одного разговора мало для изменения.", "changes": []},
            ),
            completed=True,
        ),
        context={"extensions": rt.registry},
    )
    await put(rt.store, rt.registry, Owner.PERSONA, scope, "w-reflection", record, None, rt.readers)


def test_rollbacks_closed_grounds_and_routine_episodes_shape_the_biography(tmp_path):
    async def scenario():
        async with prepared(tmp_path, [answer(reply="Ответ.")]) as env:
            await start(env)
            allow_memory(env)
            seen = await source(env, "seen", "Ника рассказала про три окна башни.")
            closed = await source(env, "closed", "Закрытая история.", transfer=False)
            await change(env, "kept", 0.75, 0.77, "Интерес к деталям вырос.", [seen])
            await change(env, "undone", 0.77, 0.79, "Поспешный вывод.", [seen])
            await change(
                env,
                "back",
                0.79,
                0.77,
                "Откат.",
                [seen],
                kind="rollback",
                revision_of="change-undone",
            )
            await change(env, "closed", 0.77, 0.78, "Из закрытой истории.", [closed])
            await episode(env, "routine", "Ответил оператору.", "Доставлено.", [seen])
            await episode(env, "walk", "Пошла к маяку.", "Нашла фонарик.", [seen])
            await kept(env, [seen])
            await send(env, "q1", "Как ты менялась?")
            user = env.world.provider.calls[-1].messages[-1].content
            return [m for m in view({"messages": [{"content": user}]})["memory"] if m["by"] in BIO]

    shown = asyncio.run(scenario())
    labels = [(m["by"], m["text"]) for m in shown]
    assert labels == [
        ("откат-изменения", "curiosity: 0.79 → 0.77. Оператор отменил изменение."),
        (
            "изменение-характера",
            "curiosity: 0.75 → 0.77 (уверенность 0.6). Интерес к деталям вырос.",
        ),
        ("событие", "Пошла к маяку. → Нашла фонарик."),
        ("вывод-рефлексии-оставить", "Одного разговора мало для изменения."),
    ]
    assert all(m["at"] for m in shown)
    total = sum(len(m["text"]) for m in shown)
    assert total <= biography.BUDGET_TOKENS * 3  # bounded
