"""Fixtures for M2-06: real SQLite, memory/recall, social memory and a scripted M2-04 gateway."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.dialogue import (
    ContextBuilder,
    ControlSnapshot,
    DialogueController,
    Signal,
    SourceRights,
)
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    LLMGateway,
    ResolvedAssignment,
    Started,
    UsageReported,
)
from ai_agent_application.memory import IndexHealth, MemoryService, RecallService, SourceDraft
from ai_agent_application.social import SocialService, register_social_extensions
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import TokenUsage
from ai_agent_contracts.persona import CharacterState, Profile
from ai_agent_contracts.states import ActivityState, DeliveryState, Provenance, RunState

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)

MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
SOCIAL = Principal("owner-social", "core_owner", Owner.SOCIAL)
CHANNEL = Principal("owner-channel", "core_owner", Owner.CHANNEL)
GRANTS = (
    Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True),
    Grant(SOCIAL, Owner.SOCIAL, read=True, write=True),
    Grant(SOCIAL, Owner.MEMORY, read=True),
    Grant(SOCIAL, Owner.CHANNEL, read=True),
    Grant(CHANNEL, Owner.CHANNEL, read=True, write=True),
)
_ids = count(1)


def run(coroutine):
    return asyncio.run(coroutine)


def eid(prefix="evt") -> str:
    return f"{prefix}-{next(_ids)}"


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


def base_scope(mode="work", persona="persona-a"):
    return make_scope(mode=mode, persona=persona).model_copy(
        update={"agent_id": None, "run_id": None, "session_id": None}
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class Control:
    """Mutable supervisor view: tests flip it like STOP / pause / loss of the epoch would."""

    def __init__(self) -> None:
        self.run_state = RunState.ACTIVE
        self.activity = ActivityState.OPEN
        self.autonomy = "autonomous"
        self.admitted = True

    def snapshot(self) -> ControlSnapshot:
        return ControlSnapshot(self.run_state, self.activity, self.autonomy, self.admitted)

    def stop(self) -> None:
        self.run_state, self.admitted = RunState.STOPPED, False


class Stream:
    def __init__(self) -> None:
        self.text: dict[str, str] = {}
        self.closed: dict[str, str] = {}

    async def emit(self, event_id: str, text: str) -> None:
        assert event_id not in self.closed, "text after the stream was closed"
        self.text[event_id] = self.text.get(event_id, "") + text

    async def close(self, event_id: str, status: str) -> None:
        self.closed[event_id] = status


class Proposals:
    def __init__(self) -> None:
        self.items = []

    async def submit(self, proposal) -> None:
        self.items.append(proposal)


def decision(reply=None, **extra) -> str:
    body = {"reply": reply, **extra}
    return json.dumps(
        {k: v for k, v in body.items() if v is not None or k == "reply"}, ensure_ascii=False
    )


def script(text: str, *, chunk=7, usage=True):
    """Provider events that deliver ``text`` in small chunks, then the terminal events."""
    events = [Started("up-model")]
    events += [Delta(text[i : i + chunk]) for i in range(0, len(text), chunk)]
    events.append(Finished("stop"))
    if usage:
        events.append(
            UsageReported(
                TokenUsage(
                    input_tokens=100, output_tokens=20, source="provider", completeness="complete"
                )
            )
        )
    return events


ASSIGNMENT = ResolvedAssignment(
    assignment_id="asg-character",
    assignment_version=1,
    role_id="character",
    connection_id="conn-a",
    adapter_id="mock",
    model_id="model-x",
    endpoint_setting="AI_PLATFORM_E",
    credential_setting="AI_PLATFORM_K",
    parameters=GenerationParameters(max_output_tokens=256, timeout_seconds=5),
    capability_verification="unverified",
)


class Fixed:
    """ProfileSource: one accepted profile and its state (invented canon)."""

    def __init__(self, registry: ExtensionRegistry, scope: Scope) -> None:
        stamp = NOW
        self.profile = Profile.model_validate(
            {
                "id": scope.persona_id,
                "scope": scope.model_dump(),
                "created_at": stamp,
                "updated_at": stamp,
                "version": 1,
                "persona_id": scope.persona_id,
                "canon": "Искра — вымышленный исследователь, любит находки и держит слово.",
                "style": "Спокойный живой русский.",
                "property_definitions": {
                    "schema_id": "persona.property-definitions",
                    "data": {
                        "properties": [
                            {
                                "name": "curiosity",
                                "layer": "dispositions",
                                "title": "Любопытство",
                                "kind": "scale",
                                "minimum": 0,
                                "maximum": 1,
                            }
                        ]
                    },
                },
                "initial_values": {
                    "schema_id": "persona.initial-values",
                    "data": {"values": {"curiosity": 0.75}},
                },
                "development_bounds": {
                    "schema_id": "persona.development-bounds",
                    "data": {"bounds": []},
                },
                "accepted_revision_id": "rev-" + "a" * 64,
            },
            context={"extensions": registry},
        )
        self.state = CharacterState.model_validate(
            {
                "id": "state-" + scope.persona_id,
                "scope": scope.model_dump(),
                "created_at": stamp,
                "updated_at": stamp,
                "version": 1,
                "persona_id": scope.persona_id,
                "profile_version": 1,
                "state_version": 1,
                "values": {
                    "schema_id": "persona.state-values",
                    "data": {"values": {"curiosity": 0.75}},
                },
            },
            context={"extensions": registry},
        )

    async def get_profile(self, scope, version=None):
        return self.profile

    async def get_state(self, scope):
        return self.state


class World:
    def __init__(self, store, memory, recall, social, registry, clock, base, provider_scripts):
        self.store, self.memory, self.recall, self.social = store, memory, recall, social
        self.registry, self.clock, self.base = registry, clock, base
        self.provider = ScriptedProvider(provider_scripts)
        self.control, self.stream, self.proposals = Control(), Stream(), Proposals()
        self.attempts: list = []

    def command(self, scope=None, request_id=None):
        return MessageCommand(
            request_id=request_id or eid("req"),
            scope=scope or self.base,
            target="synthetic-target",
            type="dialogue.message",
            arguments=MessageArguments(text="Вымышленная команда."),
        )

    def controller(
        self, session="session-1", *, capabilities=("explore",), **options
    ) -> DialogueController:
        """A fresh controller over the same stores: what a new session / restart looks like."""
        scope = self.base.model_copy(
            update={"agent_id": "agent-1", "run_id": "run-1", "session_id": session}
        )
        gateway = LLMGateway(
            lambda _a: self.provider,
            lambda _r: ASSIGNMENT,
            sink=self.attempts.append,
            admission=lambda: self.control.snapshot().admitted,
            clock=self.clock,
            sleep=_no_sleep,
            jitter=lambda: 1.0,
        )
        builder = ContextBuilder(Fixed(self.registry, scope), self.social, self.recall, self.memory)
        return DialogueController(
            scope=scope,
            generator=gateway,
            context=builder,
            social=self.social,
            memory=self.memory,
            control=self.control,
            clock=self.clock,
            capabilities=lambda: capabilities,
            proposals=self.proposals,
            stream=self.stream,
            **options,
        )

    async def memory_source(self, text, *, api=True, scope=None, **kwargs) -> SourceRef:
        scope = scope or self.base
        defaults = dict(
            provenance=Provenance.THIRD_PARTY_MESSAGE,
            source_type="synthetic-chat",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=NOW - timedelta(days=1),
            content=text,
            read_allowed=True,
            api_transfer_allowed=api,
            public_disclosure_allowed=False,
            external_id=eid("ext"),
            author_id="person-x",
        )
        defaults.update(kwargs)
        made = await self.memory.register_source(self.command(scope), SourceDraft(**defaults))
        return SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )

    async def deliver(self, utterance_id: str, status: str = "DELIVERED") -> str:
        """What the channel owner would write: the Delivery fact the promise rule relies on."""
        from ai_agent_application.storage import RecordAccess as Access
        from ai_agent_contracts.api import Receipt
        from ai_agent_contracts.control import Delivery

        delivery_id = eid("dlv")
        record = Delivery.model_validate(
            {
                "id": delivery_id,
                "scope": self.base.model_dump(),
                "created_at": self.clock.now,
                "updated_at": self.clock.now,
                "version": 1,
                "utterance_id": utterance_id,
                "revision": 1,
                "channel_id": "channel-" + delivery_id,
                "attempt_id": "attempt-" + delivery_id,
                "status": DeliveryState(status),
            }
        )
        cmd = self.command(self.base)
        async with self.store.transaction(CHANNEL, self.base, cmd) as unit:
            await unit.put(record, access=(Access(SOCIAL.id),))
            await unit.complete(
                Receipt(
                    request_id=cmd.request_id,
                    scope=self.base,
                    status="completed",
                    resource_id=delivery_id,
                    version=1,
                )
            )
        return delivery_id

    def prompt(self, call=-1) -> str:
        """All text the provider received for one call (system + user)."""
        return "\n".join(m.content for m in self.provider.calls[call].messages)

    def user_prompt(self, call=-1) -> str:
        return self.provider.calls[call].messages[1].content


async def _no_sleep(_seconds: float) -> None:
    return None


def viewer(text, *, account="acct-1", name="Ника", directed=True, api=True, event=None, **kwargs):
    return Signal(
        event_id=event or eid(),
        kind="viewer_message",
        occurred_at=NOW,
        text=text,
        platform_id="twitch",
        account_id=account,
        display_name=name,
        directed=directed,
        rights=SourceRights(read=True, api_transfer=api),
        **kwargs,
    )


def operator(text, *, event=None, **kwargs):
    return Signal(
        event_id=event or eid(),
        kind="operator_message",
        occurred_at=NOW,
        text=text,
        directed=True,
        rights=SourceRights(read=True, api_transfer=True),
        **kwargs,
    )


async def new_world(tmp_path, scripts=(), *, mode="work") -> World:
    base_dir = Path(os.environ.get("M2_06_DB_DIR", str(tmp_path)))
    base_dir.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    registry = ExtensionRegistry()
    register_social_extensions(registry)
    from ai_agent_application.persona import register_persona_extensions

    register_persona_extensions(registry)
    store = SQLiteStore(base_dir / f"{uuid4()}.sqlite", clock=clock, extensions=registry)
    await store.upgrade(MIGRATIONS)
    index = SQLiteRecallIndex(base_dir / f"{uuid4()}-index.sqlite")
    health = IndexHealth()
    memory = MemoryService(
        store,
        MEMORY,
        registry,
        index=index,
        health=health,
        clock=clock,
        reader_access=(RecordAccess(SOCIAL.id),),
    )
    recall = RecallService(store, MEMORY, index, health=health, clock=clock)
    social = SocialService(store, SOCIAL, registry, clock=clock)
    base = base_scope(mode)
    await store.provision(base, GRANTS)
    return World(store, memory, recall, social, registry, clock, base, [list(s) for s in scripts])
