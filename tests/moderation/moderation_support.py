"""Fixtures for M2-07: real SQLite, the M2-04 gateway with a scripted moderator model."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.llm import (
    Delta,
    Finished,
    GenerationParameters,
    LLMGateway,
    ResolvedAssignment,
    Started,
)
from ai_agent_application.moderation import ModerationGate, Policy, Rule
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import Freshness, Utterance

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
MODERATION = Principal("owner-moderation", "core_owner", Owner.MODERATION)
GRANTS = (Grant(MODERATION, Owner.MODERATION, read=True, write=True),)
SUBJECT = "character-model"
SCOPE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="persona-a",
    memory_space_id="space-a",
    memory_mode="work",
    agent_id="agent-1",
    run_id="run-1",
    session_id="session-1",
)
POLICY = Policy(
    policy_id="policy-test",
    version=3,
    rules=(
        Rule("no-insults", "Оскорбления", "Нельзя оскорблять людей.", "revise"),
        Rule("no-threats", "Угрозы", "Нельзя угрожать.", "block"),
    ),
)


def run(coroutine):
    return asyncio.run(coroutine)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class Control:
    """Current control generation; STOP bumps the epoch, losing admission makes it None."""

    def __init__(self) -> None:
        self.epoch, self.admitted, self.policy_version = 1, True, 1

    def __call__(self) -> Freshness | None:
        if not self.admitted:
            return None
        return Freshness(
            run_id="run-1",
            control_epoch=self.epoch,
            profile_version=1,
            policy_version=self.policy_version,
        )


class Published:
    def __init__(self) -> None:
        self.items = []

    async def publish(self, utterance) -> None:
        self.items.append((utterance.utterance_id, utterance.revision, utterance.content))


def utterance(
    text="Привет, друзья!",
    *,
    revision=1,
    audience="public-chat",
    channels=("chat",),
    status="draft",
    check_id=None,
    uid="utt-1",
) -> Utterance:
    return Utterance.model_validate(
        {
            "id": f"{uid}-r{revision}",
            "scope": SCOPE.model_dump(),
            "created_at": NOW,
            "updated_at": NOW,
            "version": revision,
            "utterance_id": uid,
            "revision": revision,
            "content": text,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "audience_id": audience,
            "channel_ids": list(channels),
            "moderation_status": status,
            "moderation_check_id": check_id,
        }
    )


def approved(base: Utterance, check_id: str) -> Utterance:
    data = base.model_dump()
    data.update(moderation_status="approved", moderation_check_id=check_id)
    return Utterance.model_validate(data)


def verdict(decision="approve", rule_ids=(), fragments=(), reason="") -> list:
    body = json.dumps(
        {
            "decision": decision,
            "rule_ids": list(rule_ids),
            "fragments": [{"start": a, "end": b} for a, b in fragments],
            "reason": reason,
        },
        ensure_ascii=False,
    )
    return [
        Started("mod-model-up"),
        *(Delta(body[i : i + 9]) for i in range(0, len(body), 9)),
        Finished("stop"),
    ]


def assignment(model="mod-model", fallback=None) -> ResolvedAssignment:
    return ResolvedAssignment(
        assignment_id="asg-" + model,
        assignment_version=1,
        role_id="moderator",
        connection_id="conn-mod",
        adapter_id="mock",
        model_id=model,
        endpoint_setting="AI_PLATFORM_MOD_E",
        credential_setting="AI_PLATFORM_MOD_K",
        parameters=GenerationParameters(max_output_tokens=200, timeout_seconds=5),
        capability_verification="unverified",
        fallback=fallback,
    )


class World:
    def __init__(self, store, provider, control, clock, gate):
        self.store, self.provider, self.control, self.clock, self.gate = (
            store,
            provider,
            control,
            clock,
            gate,
        )
        self.published = Published()


async def _no_sleep(_):
    return None


async def new_world(tmp_path, scripts=(), *, moderator=None, policy=POLICY) -> World:
    base = Path(os.environ.get("M2_07_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock, control = Clock(), Control()
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock)
    await store.upgrade(MIGRATIONS)
    await store.provision(SCOPE, GRANTS)
    provider = ScriptedProvider([list(s) for s in scripts])
    chosen = moderator or assignment()
    gateway = LLMGateway(
        lambda _a: provider, lambda _r: chosen, clock=clock, sleep=_no_sleep, jitter=lambda: 1.0
    )
    gate = ModerationGate(store, MODERATION, gateway, chosen, policy, control, clock=clock)
    return World(store, provider, control, clock, gate)
