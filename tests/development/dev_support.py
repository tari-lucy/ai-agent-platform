"""Fixtures for M3-03: real SQLite, accepted profile and state, lived episodes, scripted model."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.development import (
    Candidate,
    DevelopmentController,
    MemoryEvidence,
    Reflector,
    register_development_extensions,
)
from ai_agent_application.dialogue import ControlSnapshot
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    LLMGateway,
    ResolvedAssignment,
    Started,
)
from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft
from ai_agent_application.persona import PersonaService, register_persona_extensions
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import CreateCommand, MessageArguments, MessageCommand
from ai_agent_contracts.codec import from_wire
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ActivityState, Provenance, RunState
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
PERSONA = Principal("owner-persona", "core_owner", Owner.PERSONA)
MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
GRANTS = (
    Grant(PERSONA, Owner.PERSONA, read=True, write=True),
    Grant(PERSONA, Owner.MEMORY, read=True),
    Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True),
)
_ids = count(1)


def _scale(name, layer, title):
    return {
        "name": name,
        "layer": layer,
        "title": title,
        "kind": "scale",
        "minimum": 0,
        "maximum": 1,
    }


def _bound(name, autonomous, episodes=1):
    if not autonomous:
        return {"name": name, "autonomous": False}
    return {
        "name": name,
        "autonomous": True,
        "max_step_per_episode": 0.02,
        "max_step_per_window": 0.05,
        "window_days": 7,
        "min_independent_episodes": episodes,
    }


def _block(value) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=1) + "\n```"


PROFILE = "\n\n".join(
    [
        "<!-- ai-agent-persona-profile/1 -->",
        "# Искра (вымышленная)",
        "## Канон\n\nВымышленный исследователь.",
        "## Стиль\n\nСпокойный русский.",
        "## Свойства\n\n"
        + _block(
            {
                "properties": [
                    _scale("curiosity", "dispositions", "Любопытство"),
                    _scale("humor", "dispositions", "Юмор"),
                    _scale("openness", "beliefs", "Открытость"),
                    _scale("honesty", "identity", "Честность"),
                    {
                        "name": "habit_note",
                        "layer": "habits",
                        "title": "Заметка о привычке",
                        "kind": "text",
                        "max_chars": 200,
                    },
                ]
            }
        ),
        "## Начальные значения\n\n"
        + _block(
            {
                "values": {
                    "curiosity": 0.75,
                    "humor": 0.6,
                    "openness": 0.5,
                    "honesty": 0.9,
                    "habit_note": "нет",
                }
            }
        ),
        "## Границы развития\n\n"
        + _block(
            {
                "bounds": [
                    _bound("curiosity", True),
                    _bound("humor", True, 2),
                    _bound("openness", False),
                    _bound("honesty", False),
                    _bound("habit_note", False),
                ]
            }
        ),
    ]
)


def run(coroutine):
    return asyncio.run(coroutine)


def make_scope(mode="work", persona="persona-a"):
    return Scope(
        workspace_id="synthetic-workspace",
        persona_id=persona,
        memory_space_id=f"space-{persona}-{mode}",
        memory_mode=mode,
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class Control:
    def __init__(self) -> None:
        self.run_state, self.activity = RunState.ACTIVE, ActivityState.OPEN
        self.autonomy, self.admitted = "autonomous", True

    def __call__(self) -> ControlSnapshot:
        return ControlSnapshot(self.run_state, self.activity, self.autonomy, self.admitted)


ASSIGNMENT = ResolvedAssignment(
    assignment_id="asg-reflection",
    assignment_version=1,
    role_id="reflection",
    connection_id="conn-a",
    adapter_id="mock",
    model_id="model-x",
    endpoint_setting="AI_PLATFORM_E",
    credential_setting="AI_PLATFORM_K",
    parameters=GenerationParameters(max_output_tokens=500, timeout_seconds=5),
    capability_verification="unverified",
)


async def _no_sleep(_):
    return None


def answer(**body) -> list:
    text = json.dumps(body, ensure_ascii=False)
    return [
        Started("up"),
        *(Delta(text[i : i + 13]) for i in range(0, len(text), 13)),
        Finished("stop"),
    ]


def idea(prop="curiosity", value=0.77, episodes=("E",), uncertainty=0.3):
    return {
        "property": prop,
        "new_value": value,
        "interpretation": "Опыт показал пользу любопытства.",
        "alternative": "Возможно, это было везение.",
        "uncertainty": uncertainty,
        "expected_manifestation": "Чаще задаёт уточняющие вопросы.",
        "episode_ids": list(episodes),
    }


class World:
    def __init__(
        self, store, memory, persona, controller, control, clock, scope, provider, registry
    ):
        self.store, self.memory, self.persona, self.controller = store, memory, persona, controller
        self.control, self.clock, self.scope, self.provider = control, clock, scope, provider
        self.registry = registry
        gateway = LLMGateway(
            lambda _a: provider, lambda _r: ASSIGNMENT, sleep=_no_sleep, jitter=lambda: 1.0
        )
        self.reflector = Reflector(controller, gateway, clock=clock)

    def command(self):
        return MessageCommand(
            request_id=f"req-{next(_ids)}",
            scope=self.scope,
            target="t",
            type="dialogue.message",
            arguments=MessageArguments(text="x"),
        )

    async def episode(
        self,
        text="Мы вместе исследовали пещеру.",
        *,
        source_type="game-event",
        provenance=Provenance.OBSERVATION,
        mode_provenance=None,
    ) -> str:
        """One lived experience: a first-hand source and an episode over it; returns its id."""
        made = await self.memory.register_source(
            self.command(),
            SourceDraft(
                provenance=provenance,
                source_type=source_type,
                adapter_id="fixture",
                adapter_version="1",
                occurred_at=self.clock.now - timedelta(minutes=5),
                content=text + f" #{next(_ids)}",
                read_allowed=True,
                api_transfer_allowed=True,
                public_disclosure_allowed=False,
                external_id=f"ext-{next(_ids)}",
                author_id="person-nika",
            ),
        )
        ref = SourceRef(
            source_id=made.source_id,
            version=made.version,
            scope=self.scope,
            availability="available",
        )
        eid, _ = await self.memory.record_episode(
            self.command(),
            EpisodeDraft(
                sources=(ref,),
                participant_ids=("person-nika",),
                choice="Пошла исследовать.",
                actual_outcome="Нашла проход.",
                operation_id=f"op-{next(_ids)}",
                provenance=mode_provenance
                or ("simulation" if self.scope.memory_mode == "test" else "observation"),
            ),
        )
        return str(eid)

    async def state(self):
        return await self.persona.get_state(self.scope)

    async def candidate(self, prop="curiosity", value=0.77, episodes=(), **kwargs) -> Candidate:
        state = await self.state()
        return Candidate(
            proposal_id=kwargs.pop("proposal_id", f"prop-{next(_ids)}"),
            property=prop,
            new_value=value,
            interpretation=kwargs.pop("interpretation", "Опыт показал пользу."),
            alternative="Возможно, везение.",
            uncertainty=kwargs.pop("uncertainty", 0.3),
            expected_manifestation=kwargs.pop("expected_manifestation", "Больше вопросов."),
            episode_ids=tuple(episodes),
            base_version=kwargs.pop("base_version", state.version),
            **kwargs,
        )


async def new_world(tmp_path, scripts=(), *, mode="work", **options) -> World:
    base = Path(os.environ.get("M3_03_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    registry = ExtensionRegistry()
    register_persona_extensions(registry)
    register_development_extensions(registry)
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock, extensions=registry)
    await store.upgrade(MIGRATIONS)
    scope = make_scope(mode)
    await store.provision(scope, GRANTS)
    memory = MemoryService(
        store, MEMORY, registry, clock=clock, reader_access=(RecordAccess(PERSONA.id),)
    )
    persona = PersonaService(store, PERSONA, registry, clock=clock)
    payload = {
        "request_id": "create-1",
        "scope": scope.model_dump(mode="json"),
        "target": scope.persona_id,
        "type": "persona.create",
        "arguments": {
            "schema_id": "persona.create-args",
            "schema_version": "1.0",
            "data": {"markdown": PROFILE},
        },
    }
    await persona.create(
        from_wire(TypeAdapter(CreateCommand), json.dumps(payload).encode(), extensions=registry)
    )
    control = Control()
    controller = DevelopmentController(
        storage=store,
        principal=PERSONA,
        extensions=registry,
        evidence=MemoryEvidence(memory),
        control=control,
        clock=clock,
        **options,
    )
    provider = ScriptedProvider([list(s) for s in scripts])
    return World(store, memory, persona, controller, control, clock, scope, provider, registry)
