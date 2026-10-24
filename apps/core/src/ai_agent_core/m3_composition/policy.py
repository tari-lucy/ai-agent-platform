"""Current permissions, pinned bindings and source rights; no model can grant them."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import base_visible, scope_key, stamp
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import ContextApproval
from ai_agent_application.jobs.ports import ExecutionPolicy
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
        capability, permission, _ = await self.permit(command.scope, args.request.kind)
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
        for role in needed:
            assignment = current[role]
            binding = await self.state.binding(command.scope, assignment)
            conn = await self.store.repository(
                Connection, PRINCIPALS[Owner.WORKSPACE], command.scope
            ).get(binding.connection_id)
            if (
                conn is None
                or args.connections.get(conn.id) != conn.version
                or binding.adapter_id not in self.allowed_adapters
                or assignment.fallback_assignment_id is not None
            ):
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        for ref in command.sources:
            source = await self.store.repository(
                Source, PRINCIPALS[Owner.MEMORY], command.scope
            ).get(ref.source_id, purpose="api_transfer")
            if source is None or source.version != ref.version:
                raise ContractError(ErrorCode.ACCESS_DENIED)
        self.supervisor.resolve(
            command.cancellation_id, scope=command.scope, freshness=command.freshness
        )
        return ExecutionPolicy(command.id, command.scope, True, self.supervisor.clock.elapsed() + 2)

    async def permit(self, scope: Scope, kind: str) -> tuple[Capability, Permission, PermitLimits]:
        capability, permissions = await asyncio.gather(
            self.store.repository(Capability, PRINCIPALS[Owner.WORKSPACE], scope).get("m3." + kind),
            self.store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope).list(limit=200),
        )
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
        # JobOwner reconciles old epochs on open. No external effects are dispatched by M3.
        return tuple(previous.unconfirmed_resource_ids)
