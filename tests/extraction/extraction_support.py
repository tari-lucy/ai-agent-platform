"""Fixtures for M3-01: real SQLite memory, M2-04 gateway with a scripted extractor model."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.extraction import Consolidator, Extractor
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    LLMGateway,
    ResolvedAssignment,
    Started,
)
from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.api import MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.states import Provenance

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
GRANTS = (Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True),)
_ids = count(1)


def run(coroutine):
    return asyncio.run(coroutine)


def make_scope(mode="work", persona="persona-a"):
    return Scope(
        workspace_id="synthetic-workspace",
        persona_id=persona,
        memory_space_id=f"space-{persona}-{mode}",
        memory_mode=mode,
    )


def proposal(
    source_id,
    quote,
    claim="Ника построила замок.",
    *,
    type_="observed",
    confidence=0.9,
    subject="person-nika",
    contradicts=(),
    extra_evidence=(),
):
    evidence = [{"source_id": source_id, "quote": quote}, *extra_evidence]
    return {
        "subject_id": subject,
        "claim": claim,
        "proposed_type": type_,
        "confidence": confidence,
        "evidence": evidence,
        "contradicts": list(contradicts),
    }


def answer(*proposals) -> list:
    body = json.dumps({"proposals": list(proposals)}, ensure_ascii=False)
    return [
        Started("up"),
        *(Delta(body[i : i + 11]) for i in range(0, len(body), 11)),
        Finished("stop"),
    ]


ASSIGNMENT = ResolvedAssignment(
    assignment_id="asg-extractor",
    assignment_version=1,
    role_id="extractor",
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


class World:
    def __init__(self, store, memory, scope, provider, state):
        self.store, self.memory, self.scope, self.provider = store, memory, scope, provider
        self.state = state
        gateway = LLMGateway(
            lambda _a: provider, lambda _r: ASSIGNMENT, sleep=_no_sleep, jitter=lambda: 1.0
        )
        self.extractor = Extractor(memory, gateway, admitted=lambda: state["admitted"])
        self.consolidator = Consolidator(
            memory, gateway, admitted=lambda: state["admitted"], clock=lambda: NOW
        )

    def command(self, scope=None):
        return MessageCommand(
            request_id=f"req-{next(_ids)}",
            scope=scope or self.scope,
            target="t",
            type="dialogue.message",
            arguments=MessageArguments(text="x"),
        )

    async def source(
        self,
        text,
        *,
        provenance=Provenance.THIRD_PARTY_MESSAGE,
        source_type="synthetic-chat",
        api=True,
        author="person-nika",
        scope=None,
    ) -> SourceRef:
        scope = scope or self.scope
        made = await self.memory.register_source(
            self.command(scope),
            SourceDraft(
                provenance=provenance,
                source_type=source_type,
                adapter_id="fixture",
                adapter_version="1",
                occurred_at=NOW - timedelta(hours=1),
                content=text,
                read_allowed=True,
                api_transfer_allowed=api,
                public_disclosure_allowed=False,
                external_id=f"ext-{next(_ids)}",
                author_id=author,
            ),
        )
        return SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )

    async def episode(self, *refs: SourceRef):
        await self.memory.record_episode(
            self.command(),
            EpisodeDraft(
                sources=tuple(refs),
                participant_ids=("person-nika",),
                choice="Выбор.",
                actual_outcome="Исход.",
                operation_id=f"op-{next(_ids)}",
                provenance="simulation" if self.scope.memory_mode == "test" else "observation",
            ),
        )

    async def claims(self):
        from ai_agent_contracts.memory import Claim

        repo = self.store.repository(Claim, MEMORY, self.scope)
        return await repo.list(limit=200)

    def prompt(self):
        return "\n".join(m.content for m in self.provider.calls[-1].messages)


async def new_world(tmp_path, scripts=(), *, mode="work") -> World:
    base = Path(os.environ.get("M3_01_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=lambda: NOW)
    await store.upgrade(MIGRATIONS)
    scope = make_scope(mode)
    await store.provision(scope, GRANTS)
    memory = MemoryService(store, MEMORY, store.extensions, clock=lambda: NOW)
    provider = ScriptedProvider([list(s) for s in scripts])
    return World(store, memory, scope, provider, {"admitted": True})
