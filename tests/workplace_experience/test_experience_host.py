"""AW-06 on the production composition: character shapes the goal, results become experience.

Real build (ASGI), SQLite, memory/persona owners, deletion ledger and file owner; scripted
synthetic transport (no network, no paid calls). The agent chooses an own goal with its brief
in view, asks for and gets the skills area, saves a method and finishes: the saved result is
one observation basis and one episode; its conclusion stays an interpretation.
"""

import asyncio
import json
from dataclasses import replace

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkTask
from ai_agent_contracts.memory import Episode, Source
from test_activity import FAST, WorkModel, fast, prepared, rows, until
from test_library_tools import SKILLS, grant, use

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import Profiles
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.workplace_experience import recorder


def test_own_goal_from_character_and_results_returned_as_experience(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger" / "events.sqlite",
        )
        await enable_workplace_files(host.root, ceiling_days=7)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            rt.activity = replace(FAST, own_goals=True, own_goal_daily=1)
            save = use(
                SKILLS,
                "Сохраню свой метод.",
                op="save",
                skill_id="sketch",
                title="Набросок маяка",
                applicability="Нужно быстро зарисовать маяк.",
                method="1. Контур башни.\n2. Свет.",
            )
            model.queues["activity"] += [
                dict(action="goal", goal="Записать свой метод наброска.", reason="Мне интересно."),
                save,  # no right yet: a capability ticket
                save,
                dict(action="complete", summary="Метод записан."),
            ]
            await host.start()
            [own] = await until(
                lambda: rows(rt, WorkTask, scope),
                lambda found: len(found) == 1 and found[0].state == "waiting",
                "ask for skills",
            )
            # The goal was chosen with the person's character in view, not a fixed activity list.
            profile = await Profiles(rt).get_profile(scope)
            goal_call = json.loads(model.calls[0][1]["messages"][1]["content"])
            assert profile.canon[:60] in goal_call["persona"]
            ticket = await rt.tickets.get(scope, own.waiting_ticket_ids[0])
            await grant(host, ticket, ["skills"])
            done = await until(
                lambda: rows(rt, WorkTask, scope),
                lambda found: found[0].state == "done",
                "done",
                seconds=30,
            )
            [task] = done
            step_call = json.loads(model.last("activity")["messages"][1]["content"])
            assert profile.canon[:60] in step_call["persona"]  # the brief is in every step

            memory = PRINCIPALS[Owner.MEMORY]
            # The step's records follow its task update; wait for the conclusion as well.
            sources = await until(
                lambda: rt.store.repository(Source, memory, scope).list(limit=200),
                lambda found: any(s.source_type == "workplace-conclusion" for s in found),
                "conclusion recorded",
            )
            results = [s for s in sources if s.source_type == "workplace-result"]
            conclusions = [s for s in sources if s.source_type == "workplace-conclusion"]
            assert len(results) == 1 and results[0].provenance == "observation"
            assert (
                "sketch" in results[0].content
                and "Записано владельцем файлов" in results[0].content
            )
            assert len(conclusions) == 1 and conclusions[0].provenance == "interpretation"
            episodes = [
                e
                for e in await rt.store.repository(Episode, memory, scope).list(limit=200)
                if e.operation_id.startswith("work-")
            ]
            assert len(episodes) == 1
            assert episodes[0].experience_source_ids == [results[0].id]
            assert conclusions[0].id not in {r.source_id for r in episodes[0].sources}

            # A replay of the recorded step adds no second basis.
            await recorder(rt).step(task, task.progress[-1])
            again = await rt.store.repository(Source, memory, scope).list(limit=200)
            assert len([s for s in again if s.source_type == "workplace-result"]) == 1
            episodes_again = await rt.store.repository(Episode, memory, scope).list(limit=200)
            assert len([e for e in episodes_again if e.operation_id.startswith("work-")]) == 1

    asyncio.run(scenario())
