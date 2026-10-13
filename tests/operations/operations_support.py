"""Synthetic control/context only; no provider, secrets, working memory or IO."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import (
    Clock,
    ContextApproval,
    ControlAuthority,
    ControlLimits,
)
from ai_agent_application.scheduling import Demand, Lane, ResourceScheduler, SchedulerLimits
from ai_agent_application.telemetry import Telemetry, TelemetryLimits
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from ai_agent_contracts.common import Contract, ExtensionPayload, Scope
from ai_agent_contracts.control import ExecutionCommand
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.workspace import Capability
from pydantic import TypeAdapter

BASE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-test",
    memory_mode="test",
)


class Time:
    def __init__(self):
        self.value = 0.0
        self.now = datetime(2026, 10, 13, tzinfo=UTC)

    def clock(self):
        return Clock(lambda: self.value, lambda: self.now)

    def advance(self, seconds):
        self.value += seconds
        self.now += timedelta(seconds=seconds)


class Journal:
    async def load(self):
        return []

    async def save(self, value):
        pass


class Readiness:
    def __init__(self, time):
        self.time = time

    async def check(self, run):
        return ContextApproval(run.scope, 1, 1, self.time.value + 60)

    async def reconcile(self, previous):
        return ()


class Empty(Contract):
    pass


class Fixture:
    async def setup(self, *, limits=None, pools=None, telemetry_limits=None):
        self.time = Time()
        self.supervisor = Supervisor(
            BASE,
            "synthetic-agent",
            Journal(),
            Readiness(self.time),
            clock=self.time.clock(),
            limits=ControlLimits(sleep_gap_seconds=60),
        )
        self.operator = ControlAuthority("synthetic-operator", frozenset({BASE.partition}), 3600)
        self.telemetry = Telemetry(
            clock=self.time.clock(),
            limits=telemetry_limits or TelemetryLimits(),
            critical_failure=self.supervisor.critical_failure,
        )
        options = {} if pools is None else {"pools": pools}
        self.scheduler = ResourceScheduler(
            self.supervisor, self.telemetry, limits=limits or SchedulerLimits(), **options
        )
        await self.supervisor.recover()
        await self.supervisor.control(
            self.operator,
            ControlCommand(
                request_id="start",
                type="agent.start",
                target="synthetic-agent",
                scope=self.supervisor.snapshot.scope,
                arguments=ControlArguments(autonomy="autonomous"),
                expected_version=self.supervisor.snapshot.version,
            ),
        )
        registry = ExtensionRegistry()
        self.registry = registry
        registry.register("synthetic.empty", Empty)
        self.empty = TypeAdapter(ExtensionPayload).validate_python(
            {"schema_id": "synthetic.empty", "data": {}}, context={"extensions": registry}
        )
        return self

    def capability(self, identifier, demands, lane=Lane.FOREGROUND, *, version=1, components=()):
        cap = Capability(
            id=identifier,
            scope=self.supervisor.snapshot.scope,
            created_at=self.time.now,
            updated_at=self.time.now,
            version=version,
            adapter_id="synthetic-adapter",
            adapter_version="1",
            input_schema_id="synthetic.empty",
            output_schema_id="synthetic.empty",
            effect="none",
            required_permissions=[],
            resource_ids=list(demands),
            timeout_ms=90000,
            cancellation_supported=True,
            idempotency="supported",
            health="ready",
            setting_references=[],
        )
        self.scheduler.register(
            cap,
            tuple(Demand(name, units) for name, units in demands.items()),
            lane,
            components=components,
        )
        return cap

    def command(self, cap, *, seconds=90):
        admission = self.supervisor.admit("activity")
        return ExecutionCommand.model_validate(
            dict(
                id=uuid4().hex,
                scope=admission.scope,
                created_at=self.time.now,
                type="execution.command",
                operation_id=uuid4().hex,
                target="synthetic-worker",
                binding_id="synthetic-binding",
                capability_id=cap.id,
                capability_version=cap.version,
                arguments=self.empty,
                permission_id="synthetic-permission",
                permission_version=1,
                deadline=self.time.now + timedelta(seconds=seconds),
                conditions=self.empty,
                cancellation_id=admission.id,
                freshness=admission.freshness,
                sources=[],
            ),
            context={"extensions": self.registry},
        )

    def acquire(self, cap):
        request = self.scheduler.request(self.command(cap))
        allocation = self.scheduler.acquire(request)
        assert allocation is not None and allocation.acquired
        return allocation.lease

    def stop(self):
        return self.supervisor.stop(
            self.operator,
            StopCommand(
                request_id=uuid4().hex,
                type="agent.stop",
                target="synthetic-agent",
                scope=self.supervisor.snapshot.scope,
            ),
        )


def fixture(**options):
    return asyncio.run(Fixture().setup(**options))
