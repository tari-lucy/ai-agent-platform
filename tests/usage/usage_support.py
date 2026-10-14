"""Fixtures for M2-05: real SQLite authority, invented operations and prices only."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.llm import AttemptReport
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_application.usage import TariffDraft, UsageLedger
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.common import Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import LLMAssignment, TokenUsage

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)

LLM = Principal("owner-llm", "core_owner", Owner.LLM)
OPERATOR = Principal("operator-synthetic", "operator")
WORKER = Principal("worker-synthetic", "worker")
GRANTS = (Grant(LLM, Owner.LLM, read=True, write=True),)
_ids = count(1)


def run(coroutine):
    return asyncio.run(coroutine)


def rid() -> str:
    return f"req-{next(_ids)}"


def make_scope(persona="persona-a", mode="work"):
    return Scope(
        workspace_id="synthetic-workspace",
        persona_id=persona,
        memory_space_id=f"space-{persona}-{mode}",
        memory_mode=mode,
    )


def command(scope, request_id=None):
    return MessageCommand(
        request_id=request_id or rid(),
        scope=scope,
        target="synthetic-target",
        type="dialogue.message",
        arguments=MessageArguments(text="Вымышленная команда."),
    )


def usage(input_tokens=1000, output_tokens=500, cached=None, reasoning=None, *, partial=False):
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=None if partial else output_tokens,
        cached_input_tokens=cached,
        reasoning_output_tokens=None if partial else reasoning,
        source="provider",
        completeness="partial" if partial else "complete",
    )


UNKNOWN = TokenUsage(
    input_tokens=None, output_tokens=None, source="unknown", completeness="unknown"
)


def report(
    attempt="op-1.a1",
    *,
    operation="op-1",
    status="completed",
    usage_=None,
    model="model-x",
    reported=None,
    role="character",
    assignment="asg-a",
    version=1,
    at=NOW,
):
    return AttemptReport(
        operation_id=operation,
        attempt_id=attempt,
        role_id=role,
        assignment_id=assignment,
        assignment_version=version,
        connection_id="conn-a",
        requested_model_id=model,
        reported_model_id=reported,
        status=status,
        failure=None,
        started_at=at,
        completed_at=at + timedelta(seconds=2),
        usage=usage_ if usage_ is not None else usage(),
        delivered_chars=10,
    )


def draft(model="model-x", connection="conn-a", *, effective=NOW - timedelta(days=1), **rates):
    base = {"input_rate": "2", "output_rate": "6"}
    base.update(rates)
    return TariffDraft(
        connection_id=connection,
        model_id=model,
        unit_tokens=1000,
        currency=base.pop("currency", "USD"),
        effective_at=effective,
        **base,
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW + timedelta(hours=1)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class World:
    def __init__(self, store, ledger, clock, registry):
        self.store, self.ledger, self.clock, self.registry = store, ledger, clock, registry

    def restarted(self) -> UsageLedger:
        return UsageLedger(self.store, LLM, self.registry, clock=self.clock)

    async def assignment(
        self, scope, name="asg-a", *, connection="conn-a", model="model-x", version=1
    ):
        """The role binding the attempts refer to (owned by the LLM owner)."""
        stamp = NOW
        record = LLMAssignment.model_validate(
            {
                "id": name,
                "scope": scope.model_dump(),
                "created_at": stamp,
                "version": version,
                "updated_at": stamp,
                "role_id": "character",
                "connection_id": connection,
                "model_id": model,
                "parameters": {
                    "schema_id": "usage.fixture-parameters",
                    "data": {"max_output_tokens": 256},
                },
                "required_capabilities": [],
                "capability_verification": "unverified",
            },
            context={"extensions": self.registry},
        )
        cmd = command(scope)
        async with self.store.transaction(LLM, scope, cmd) as unit:
            await unit.put(record, expected_version=None if version == 1 else version - 1)
            await unit.complete(
                Receipt(
                    request_id=cmd.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=name,
                    version=version,
                )
            )

    async def record(self, scope, item):
        return await self.ledger.record_attempt(command(scope), item)


async def new_world(tmp_path, scopes, *, assignments=True) -> World:
    from ai_agent_contracts.common import Contract

    class Fixture(Contract):
        max_output_tokens: int

    base = Path(os.environ.get("M2_05_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    registry = ExtensionRegistry()
    registry.register("usage.fixture-parameters", Fixture)
    store = SQLiteStore(base / f"{uuid4()}.sqlite", clock=clock, extensions=registry)
    await store.upgrade(MIGRATIONS)
    world = World(store, UsageLedger(store, LLM, registry, clock=clock), clock, registry)
    for scope in scopes:
        await store.provision(scope, GRANTS)
        if assignments:
            await world.assignment(scope)
    return world
