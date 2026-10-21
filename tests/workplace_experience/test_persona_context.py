"""AW-06 persona brief: only transferable obligations and memories reach the model.

Port doubles with the published shapes (profile, state, obligations, recall, source rights);
the real owners are exercised in test_experience_host.py.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace as NS

from ai_agent_application.agent_experience import BriefLimits, PersonaContext
from ai_agent_contracts.common import Scope, SourceRef

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="s", memory_mode="work")


def ref(name: str) -> SourceRef:
    return SourceRef(source_id=name, version=1, scope=SCOPE, availability="available")


class Ports:
    def __init__(self) -> None:
        self.sources = {
            "open": NS(availability="available", api_transfer_allowed=True),
            "private": NS(availability="available", api_transfer_allowed=False),
            "gone": NS(availability="deleted", api_transfer_allowed=True),
        }
        self.queries: list[str] = []

    async def get_profile(self, scope, version=None):
        return NS(canon="Любит маяки и <старые> карты.", style="Коротко.")

    async def get_state(self, scope):
        return NS(values=NS(data={"values": {"настроение": "спокойное"}}))

    async def get_source(self, scope, source_id):
        return self.sources.get(source_id)

    async def obligations(self, scope, person_id=None):
        deadline = datetime(2026, 11, 1, tzinfo=UTC)
        return [
            NS(promise=NS(content="Прислать конспект", deadline=deadline, sources=[ref("open")])),
            NS(promise=NS(content="Тайное обещание", deadline=None, sources=[ref("private")])),
            NS(promise=NS(content="Без основания", deadline=None, sources=[])),
        ]

    async def recall(self, request):
        assert request.purpose == "api_transfer"
        self.queries.append(request.query)
        return NS(
            fragments=[
                NS(source=ref("open"), content="Маяк на мысе зажгли в 1850."),
                NS(source=ref("private"), content="Личное письмо."),
                NS(source=ref("gone"), content="Удалённое."),
            ]
        )


def test_brief_has_character_obligations_and_memory_within_rights():
    ports = Ports()
    brief = asyncio.run(
        PersonaContext(ports, ports, ports, ports, BriefLimits()).build(SCOPE, "маяки")
    )
    assert "Любит маяки" in brief.text and "‹старые›" in brief.text  # data, not markup
    assert "настроение=спокойное" in brief.text
    assert "Прислать конспект (срок 2026-11-01)" in brief.text
    assert "Маяк на мысе" in brief.text
    for hidden in ("Тайное обещание", "Без основания", "Личное письмо", "Удалённое"):
        assert hidden not in brief.text
    assert brief.omitted == 4
    assert [r.source_id for r in brief.sources] == ["open"]  # these become attempt inputs
    assert ports.queries == ["маяки"]


def test_no_profile_means_no_brief():
    ports = Ports()

    async def none(scope, version=None):
        return None

    ports.get_profile = none
    brief = asyncio.run(PersonaContext(ports, ports).build(SCOPE, "x"))
    assert (brief.text, brief.sources) == ("", ())


# --- results returned to memory -------------------------------------------------------------

from ai_agent_application.agent_experience import ExperienceRecorder  # noqa: E402
from ai_agent_contracts.agent_workplace import WorkStep, WorkTask  # noqa: E402
from ai_agent_contracts.common import ArtifactRef  # noqa: E402
from ai_agent_contracts.states import Provenance  # noqa: E402

NOW = datetime(2026, 10, 21, tzinfo=UTC)


class Memory:
    """Memory owner double: idempotent by request id like the real one."""

    def __init__(self) -> None:
        self.sources: dict[str, tuple] = {}
        self.episodes: dict[str, object] = {}

    async def get_source(self, scope, source_id):
        return NS(availability="available", api_transfer_allowed=True)

    async def register_source(self, command, draft):
        self.sources.setdefault(command.request_id, (command.scope, draft))
        return NS(source_id="src-" + command.request_id, version=1)

    async def record_episode(self, command, draft):
        key = "ep-" + draft.operation_id
        duplicate = key in self.episodes
        self.episodes.setdefault(key, (command.scope, draft))
        return key, duplicate


def work(scope: Scope, state="active", outcome=None) -> WorkTask:
    return WorkTask(
        id="t",
        task_id="t",
        scope=scope,
        created_at=NOW,
        updated_at=NOW,
        version=1,
        origin="assignment",
        goal="Конспект",
        reason="",
        state=state,
        outcome=outcome,
    )


def step(scope: Scope, status="succeeded", written=True) -> WorkStep:
    file = ArtifactRef(
        artifact_id="note",
        version=1,
        scope=scope,
        checksum="a" * 64,
        size_bytes=1,
        media_type="text/markdown",
    )
    return WorkStep(
        at=NOW,
        attempt_id="t.a0001",
        capability_id="workplace.knowledge",
        status=status,
        summary="Записано: note v1 (note).",
        written=[file] if written else [],
    )


def test_results_become_one_basis_and_conclusions_stay_interpretations():
    async def scenario():
        memory = Memory()
        recorder = ExperienceRecorder(memory, lambda: NOW)
        first = await recorder.step(work(SCOPE), step(SCOPE))
        again = await recorder.step(work(SCOPE), step(SCOPE))  # retry/replay of the attempt
        assert first == again and first.episode_id == "ep-work-t.a0001"
        assert len(memory.sources) == 1 and len(memory.episodes) == 1  # same keys: no doubling
        [(scope, draft)] = memory.sources.values()
        assert scope == SCOPE and draft.provenance is Provenance.OBSERVATION
        assert draft.external_id == "t.a0001" and "note v1" in draft.content
        # A plan, a failed step or a step without a written result is not experience.
        assert await recorder.step(work(SCOPE), step(SCOPE, status="failed")) is None
        assert await recorder.step(work(SCOPE), step(SCOPE, written=False)) is None
        done = work(SCOPE, state="done", outcome="Собственный вывод: готово.")
        concluded = await recorder.conclusion(done, "t.a0002")
        assert concluded.episode_id is None
        kinds = {d.source_type: d.provenance for _, d in memory.sources.values()}
        assert kinds["workplace-conclusion"] is Provenance.INTERPRETATION
        assert len(memory.episodes) == 1
        assert await recorder.conclusion(work(SCOPE), "t.a0003") is None  # not finished

    asyncio.run(scenario())


def test_test_partition_records_simulation_in_its_own_scope():
    async def scenario():
        memory = Memory()
        lab = SCOPE.model_copy(update={"memory_space_id": "lab", "memory_mode": "test"})
        await ExperienceRecorder(memory, lambda: NOW).step(work(lab), step(lab))
        [(scope, draft)] = memory.sources.values()
        [(episode_scope, episode)] = memory.episodes.values()
        assert scope == lab == episode_scope and draft.provenance is Provenance.SIMULATION
        assert episode.provenance == "simulation"

    asyncio.run(scenario())
