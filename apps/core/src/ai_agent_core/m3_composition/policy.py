"""Current permissions, pinned bindings and source rights; no model can grant them."""

import asyncio
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import cast

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import base_visible, scope_key, stamp
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import ContextApproval
from ai_agent_application.jobs.ports import ExecutionPolicy
from ai_agent_application.llm import ResolvedAssignment, resolve_assignment
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import ExecutionCommand, RunSession
from ai_agent_contracts.llm import Connection
from ai_agent_contracts.memory import Source
from ai_agent_contracts.persona import CharacterState, Profile
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import Capability, Workspace
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from .action_read import ActionRead, CheckedAction
from .schemas import JobArguments, PermitLimits
from .settings import TransferJournal
from .state import OPERATOR, PRINCIPALS, ROLES, State

WORKER_ID = "m3-core"


@dataclass(frozen=True)
class Context:
    profile: Profile
    workspace: Workspace
    rules_version: int


class Policy:
    def __init__(
        self,
        store: SQLiteStore,
        state: State,
        supervisor: Supervisor,
        *,
        allowed_adapters: frozenset[str],
    ) -> None:
        self.store, self.state, self.supervisor = store, state, supervisor
        self.allowed_adapters = allowed_adapters
        self.transfers = TransferJournal(state)
        self._action_read: ContextVar[ActionRead | None] = ContextVar("action_read", default=None)

    @contextmanager
    def action_read(self, command: ExecutionCommand) -> Iterator[ActionRead]:
        read = ActionRead(command)
        token = self._action_read.set(read)
        try:
            yield read
        finally:
            read.close()
            self._action_read.reset(token)

    async def context(self, scope: Scope) -> Context:
        # The same owner/ACL/TTL queries remain fresh on every admission. They
        # do not depend on one another, so avoid four serial database round trips.
        profile, states, workspace, gm = await asyncio.gather(
            self.store.repository(Profile, PRINCIPALS[Owner.PERSONA], scope).get(scope.persona_id),
            self.store.repository(CharacterState, PRINCIPALS[Owner.PERSONA], scope).list(limit=2),
            self.store.repository(Workspace, PRINCIPALS[Owner.WORKSPACE], scope).get(
                scope.workspace_id
            ),
            self.state.gm(scope),
        )
        # PersonaService uses a deterministic profile id, not necessarily persona_id.
        if profile is None:
            rows = await self.store.repository(Profile, PRINCIPALS[Owner.PERSONA], scope).list(
                limit=2
            )
            profile = rows[0] if len(rows) == 1 else None
        if (
            profile is None
            or len(states) != 1
            or workspace is None
            or states[0].profile_version != profile.version
        ):
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if await self.transfers.pending(scope):
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        return Context(profile, workspace, gm.rules_version)

    async def check(self, command: ExecutionCommand, worker_id: str | None) -> ExecutionPolicy:
        if (
            worker_id not in (None, WORKER_ID)
            or command.scope.partition != self.supervisor.snapshot.scope.partition
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        args = self.state.registry.validate(
            command.arguments.schema_id, command.arguments.schema_version, command.arguments.data
        )
        if not isinstance(args, JobArguments) or command.capability_id != "m3." + args.request.kind:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        context = await self.context(command.scope)
        if (context.profile.version, context.workspace.policy_version, context.rules_version) != (
            command.freshness.profile_version,
            command.freshness.policy_version,
            args.rules_version,
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if args.request.kind == "gm":
            gm = await self.state.gm(command.scope)
            if gm.intensity != args.gm_intensity:
                raise ContractError(ErrorCode.STALE_CONTEXT)
        capability, permission, limits = await self.permit(command.scope, args.request.kind)
        if (capability.id, capability.version, permission.id, permission.version) != (
            command.capability_id,
            command.capability_version,
            command.permission_id,
            command.permission_version,
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        current = await self.state.roles(command.scope)
        needed = ROLES[args.request.kind]
        if {current[r].id: current[r].version for r in needed if r in current} != args.roles or len(
            args.roles
        ) != len(needed):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        connections = await self.store.repository(
            Connection, PRINCIPALS[Owner.WORKSPACE], command.scope
        ).get_many([current[role].connection_id for role in needed])
        by_id = {row.id: row for row in connections}
        bindings: dict[str, ResolvedAssignment] = {}
        for role in needed:
            assignment = current[role]
            conn = by_id.get(assignment.connection_id)
            if (
                conn is None
                or args.connections.get(conn.id) != conn.version
                or assignment.fallback_assignment_id is not None
            ):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            binding = resolve_assignment(assignment, conn, self.state.registry)
            if binding.adapter_id not in self.allowed_adapters:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            bindings[role] = binding
        if command.sources:
            rows = await self.store.repository(
                Source, PRINCIPALS[Owner.MEMORY], command.scope
            ).get_many([ref.source_id for ref in command.sources], purpose="api_transfer")
            sources = {row.id: row for row in rows}
            if any(
                ref.source_id not in sources or sources[ref.source_id].version != ref.version
                for ref in command.sources
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)
        self.supervisor.resolve(
            command.cancellation_id, scope=command.scope, freshness=command.freshness
        )
        proof = ExecutionPolicy(
            command.id, command.scope, True, self.supervisor.clock.elapsed() + 2
        )
        capture = self._action_read.get()
        if capture is not None:
            capture.record(command, proof, CheckedAction(MappingProxyType(bindings), limits))
        return proof

    async def permit(self, scope: Scope, kind: str) -> tuple[Capability, Permission, PermitLimits]:
        group = await self.store.read_group(
            (Capability, Permission),
            PRINCIPALS[Owner.WORKSPACE],
            scope,
            ids={Capability: ["m3." + kind]},
        )
        capabilities = cast(list[Capability], group[Capability])
        capability = capabilities[0] if capabilities else None
        permissions = cast(list[Permission], group[Permission])
        candidates = [
            p
            for p in permissions
            if p.capability_id == "m3." + kind
            and p.principal_id == "operator"
            and p.binding_id == WORKER_ID
        ]
        if capability is None or len(candidates) != 1:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        permission = candidates[0]
        expected_resources = {"background"} if kind == "laboratory" else {"background", "llm"}
        if (
            capability.health != "ready"
            or capability.adapter_id != WORKER_ID
            or capability.effect != "local_write"
            or capability.input_schema_id != "m3.job"
            or capability.output_schema_id != "m3.result"
            or not capability.cancellation_supported
            or capability.idempotency != "supported"
            or set(capability.resource_ids) != expected_resources
            or capability.required_permissions != [permission.id]
            or permission.level != "automatic"
            or permission.revoked_at is not None
            or permission.operation_type != "m3." + kind
            or permission.expires_at <= self.supervisor.clock.utc()
            or permission.conditions.schema_id != "m3.empty"
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        limits = self.state.registry.validate(
            permission.limits.schema_id, permission.limits.schema_version, permission.limits.data
        )
        if not isinstance(limits, PermitLimits):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return capability, permission, limits

    async def transaction_check(
        self, command: ExecutionCommand, connection: AsyncConnection
    ) -> None:
        """Pins are rechecked under BEGIN IMMEDIATE, before every domain owner commit."""
        args = self.state.registry.validate(
            command.arguments.schema_id, command.arguments.schema_version, command.arguments.data
        )
        assert isinstance(args, JobArguments)
        pins = [
            ("Permission", command.permission_id, command.permission_version),
            ("Capability", command.capability_id, command.capability_version),
            *(("LLMAssignment", i, v) for i, v in args.roles.items()),
            *(("Connection", i, v) for i, v in args.connections.items()),
        ]
        for kind, identifier, version in pins:
            row = (
                await connection.execute(
                    select(
                        db.records.c.version, db.records.c.deleted, db.records.c.available
                    ).where(
                        db.records.c.scope_id == scope_key(command.scope),
                        db.records.c.kind == kind,
                        db.records.c.id == identifier,
                    )
                )
            ).first()
            if row is None or row.version != version or row.deleted or not row.available:
                raise ContractError(ErrorCode.STALE_CONTEXT)
        for kind, identifier in (
            ("Permission", command.permission_id),
            ("Workspace", command.scope.workspace_id),
            ("GmRuntimeState", "gm-runtime"),
        ):
            payload = await connection.scalar(
                select(db.revisions.c.payload)
                .select_from(
                    db.records.join(
                        db.revisions,
                        and_(
                            db.records.c.scope_id == db.revisions.c.scope_id,
                            db.records.c.kind == db.revisions.c.kind,
                            db.records.c.id == db.revisions.c.id,
                            db.records.c.version == db.revisions.c.version,
                        ),
                    )
                )
                .where(
                    db.records.c.scope_id == scope_key(command.scope),
                    db.records.c.kind == kind,
                    db.records.c.id == identifier,
                    db.records.c.deleted.is_(False),
                    db.records.c.available.is_(True),
                )
            )
            if not isinstance(payload, str):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            data = json.loads(payload)
            if kind == "Permission" and (
                data["revoked_at"] is not None
                or datetime.fromisoformat(data["expires_at"]) <= self.supervisor.clock.utc()
                or data["policy_version"] != command.freshness.policy_version
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if kind == "Workspace" and data["policy_version"] != command.freshness.policy_version:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if kind == "GmRuntimeState" and (
                data["rules_version"] != args.rules_version
                or (args.request.kind == "gm" and data["intensity"] != args.gm_intensity)
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
        for ref in command.sources:
            now = stamp(self.supervisor.clock.utc())
            checks = [base_visible(db.records, PRINCIPALS[Owner.MEMORY], "api_transfer", now)]
            if args.request.kind == "dialogue":
                checks.append(base_visible(db.records, OPERATOR, "api_transfer", now))
            allowed = await connection.scalar(
                select(db.records.c.id).where(
                    db.records.c.scope_id == scope_key(command.scope),
                    db.records.c.kind == "Source",
                    db.records.c.id == ref.source_id,
                    db.records.c.version == ref.version,
                    *checks,
                )
            )
            if allowed is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)
        # Profile id can differ from persona_id; exactly one accepted profile is required.
        versions: Sequence[int] = (
            (
                await connection.execute(
                    select(db.records.c.version).where(
                        db.records.c.scope_id == scope_key(command.scope),
                        db.records.c.kind == "Profile",
                        db.records.c.deleted.is_(False),
                        db.records.c.available.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        if list(versions) != [command.freshness.profile_version]:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.supervisor.resolve(
            command.cancellation_id, scope=command.scope, freshness=command.freshness
        )


class Readiness:
    def __init__(self, policy: Policy):
        self.policy = policy

    async def check(self, run: RunSession) -> ContextApproval:
        context = await self.policy.context(run.scope)
        return ContextApproval(
            run.scope,
            context.profile.version,
            context.workspace.policy_version,
            self.policy.supervisor.clock.elapsed() + 60,
        )

    async def reconcile(self, previous: RunSession) -> tuple[str, ...]:
        """Resources of a run that ended without receipts (a crash, a host reboot).

        JobOwner reconciles old epochs on open. The platform's own sandbox containers and
        gateways are checked in Docker and removed when left over; only what Docker cannot
        confirm stays unknown, so a reboot no longer locks START for good.
        """
        from ai_agent_adapters.sandbox.docker import reclaim

        unknown = []
        for resource_id in previous.unconfirmed_resource_ids:
            if not await reclaim(resource_id):
                unknown.append(resource_id)
        return tuple(unknown)
