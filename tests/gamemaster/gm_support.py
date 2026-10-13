"""Fixtures for M3-02: real SQLite, memory sources, a scripted game-master model."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.gamemaster import (
    Dice,
    Family,
    GameMaster,
    GmPolicy,
    StoryRule,
    StoryRules,
)
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    LLMGateway,
    ResolvedAssignment,
    Started,
)
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import Provenance

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
GM = Principal("owner-gm", "core_owner", Owner.GM)
GRANTS = (
    Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True),
    Grant(GM, Owner.GM, read=True, write=True),
    Grant(GM, Owner.MEMORY, read=True),
)
_ids = count(1)
RULES = StoryRules.layered(
    2,
    project=(
        StoryRule("no-harm", "project", "hard", "Нет насилия над людьми.", ("убить", "пытка")),
    ),
    profile=(
        StoryRule("no-romance", "profile", "soft", "Без романтических линий.", ("свидание",)),
    ),
)
FAMILIES = (
    Family("find", "Необычная находка", ("explore",), ("камень", "след"), (0.2, 0.6)),
    Family("promise", "Вернуться к обещанию", ("talk",), ("напомнить",), (0.1, 0.3)),
    Family("trade", "Обмен с игроком", ("talk", "trade"), ("мена",), (0.3, 0.7)),
)


def run(coroutine):
    return asyncio.run(coroutine)


def make_scope(session="session-1", mode="work", persona="persona-a"):
    return Scope(
        workspace_id="synthetic-workspace",
        persona_id=persona,
        memory_space_id=f"space-{persona}-{mode}",
        memory_mode=mode,
        agent_id="agent-1",
        run_id="run-1",
        session_id=session,
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


ASSIGNMENT = ResolvedAssignment(
    assignment_id="asg-gm",
    assignment_version=1,
    role_id="game-master",
    connection_id="conn-a",
    adapter_id="mock",
    model_id="model-x",
    endpoint_setting="AI_PLATFORM_E",
    credential_setting="AI_PLATFORM_K",
    parameters=GenerationParameters(max_output_tokens=400, timeout_seconds=5),
    capability_verification="unverified",
)


def draft(
    topic="old-map", applicability="Найдена карта без подписи.", boundaries="Без риска для людей."
):
    body = json.dumps(
        {"applicability": applicability, "boundaries": boundaries, "topic_key": topic},
        ensure_ascii=False,
    )
    return [
        Started("up"),
        *(Delta(body[i : i + 12]) for i in range(0, len(body), 12)),
        Finished("stop"),
    ]


async def _no_sleep(_):
    return None


class World:
    def __init__(self, store, memory, gm, provider, clock, state, scope):
        self.store, self.memory, self.gm, self.provider = store, memory, gm, provider
        self.clock, self.state, self.scope = clock, state, scope
        self.generator = LLMGateway(
            lambda _a: provider, lambda _r: ASSIGNMENT, sleep=_no_sleep, jitter=lambda: 1.0
        )

    def command(self):
        return MessageCommand(
            request_id=f"req-{next(_ids)}",
            scope=self.scope,
            target="t",
            type="dialogue.message",
            arguments=MessageArguments(text="x"),
        )

    async def source(
        self,
        text="Игрок нашёл странный камень у реки.",
        *,
        provenance=Provenance.OBSERVATION,
        source_type="game-event",
        api=True,
    ):
        made = await self.memory.register_source(
            self.command(),
            SourceDraft(
                provenance=provenance,
                source_type=source_type,
                adapter_id="fixture",
                adapter_version="1",
                occurred_at=NOW - timedelta(minutes=5),
                content=text,
                read_allowed=True,
                api_transfer_allowed=api,
                public_disclosure_allowed=False,
                external_id=f"ext-{next(_ids)}",
                author_id="person-nika",
            ),
        )
        return SourceRef(
            source_id=made.source_id,
            version=made.version,
            scope=self.scope,
            availability="available",
        )

    async def arc(self, key="river", **kwargs):
        ground = kwargs.pop("ground", None) or await self.source()
        return (
            await self.gm.open_arc(
                self.scope,
                key,
                [ground],
                kwargs.get("direction", "Исследовать реку."),
                kwargs.get("boundaries", "Мирно."),
            )
        ), ground

    async def offer(self, arc_id, ground, family="find", topic="stone", **kwargs):
        return await self.gm.propose(
            self.scope,
            arc_id,
            family,
            topic,
            kwargs.get("applicability", "Странный камень у реки."),
            kwargs.get("boundaries", "Без риска."),
            [ground],
        )

    async def records(self, model, principal=GM):
        repo = self.store.repository(model, principal, self.scope)
        return await repo.list(limit=200)


async def new_world(tmp_path, scripts=(), *, scope=None, seed="seed-1", **gm_options) -> World:
    base = Path(os.environ.get("M3_02_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock)
    await store.upgrade(MIGRATIONS)
    scope = scope or make_scope()
    await store.provision(scope, GRANTS)
    memory = MemoryService(
        store, MEMORY, store.extensions, clock=clock, reader_access=(RecordAccess(GM.id),)
    )
    state = {"admitted": True}
    gm = GameMaster(
        storage=store,
        principal=GM,
        extensions=store.extensions,
        memory=memory,
        rules=gm_options.pop("rules", RULES),
        families=gm_options.pop("families", FAMILIES),
        dice=Dice(seed),
        admitted=lambda: state["admitted"],
        clock=clock,
        **gm_options,
    )
    provider = ScriptedProvider([list(s) for s in scripts])
    return World(store, memory, gm, provider, clock, state, scope)


__all__ = ["GmPolicy"]
