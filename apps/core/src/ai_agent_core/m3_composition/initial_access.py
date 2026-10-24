"""One operator decision for the configured initial workplace (OC-05), not a wildcard right.

The set is the capabilities the host offline-configured with an automatic ceiling
(`workplace_permissions`); a public effect is never part of it. Preview pins every member
(manifest, ready binding, current right) and the workspace policy; execute rechecks all of
them and writes the members' ordinary Permissions atomically in one workspace-owner
transaction, then closes the set's open capability requests with an owner receipt. A
capability registered later does not inherit the decision. Replay returns the same receipt.
Revoking stays per right (existing permission decision). Nothing here starts the agent.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import derived_id
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    Ticket,
    TicketReplyArguments,
)
from ai_agent_contracts.api import (
    ConsequencePlan,
    EditCommand,
    ExecutePlanCommand,
    Permission,
    PlanCommand,
    PlanDependency,
    Receipt,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workplace_access import (
    InitialAccessArguments,
    InitialAccessCapability,
    InitialAccessMember,
    InitialAccessProof,
    InitialAccessStatus,
)
from ai_agent_contracts.workspace import Workspace

from .permission_decisions import HOLDER, PLAN_MINUTES
from .state import PRINCIPALS, extension, record

if TYPE_CHECKING:
    from .service import Runtime

TARGET = "initial-access"
SCHEMA = "workplace.access-args"


@dataclass(frozen=True)
class Member:
    manifest: CapabilityManifest
    binding: CapabilityBinding | None
    current: Permission | None


def permission_id(scope: Scope, capability_id: str, binding_id: str) -> str:
    # The same id as a single permission decision: the decision renews, never duplicates.
    return derived_id("workplace-permission", area(scope), capability_id + "|" + binding_id)


def _active(current: Permission | None, now: object) -> bool:
    return (
        current is not None
        and current.level == "automatic"
        and current.revoked_at is None
        and current.expires_at > now  # type: ignore[operator]
    )


async def members(rt: Runtime, scope: Scope) -> list[Member]:
    """The configured set, read from the workspace owner (manifests, bindings, rights)."""
    table = rt.permission_ceiling or {}
    owner = PRINCIPALS[Owner.WORKSPACE]
    manifests = await rt.store.repository(CapabilityManifest, owner, scope).list(limit=200)
    bindings = await rt.store.repository(CapabilityBinding, owner, scope).list(limit=200)
    rights = rt.store.repository(Permission, owner, scope)
    found: list[Member] = []
    for capability_id in sorted(table):
        if table[capability_id].level != "automatic":
            continue
        rows = [row for row in manifests if row.capability_id == capability_id]
        if len(rows) != 1 or rows[0].effect == "public_output":
            continue
        manifest = rows[0]
        ready = sorted(
            (
                row
                for row in bindings
                if row.capability_id == capability_id
                and row.state == "ready"
                and row.capability_version == manifest.capability_version
            ),
            key=lambda row: (row.verified_at or row.updated_at, row.id),
        )
        binding = ready[-1] if ready else None
        current = (
            None
            if binding is None
            else await rights.get(permission_id(scope, capability_id, binding.binding_id))
        )
        found.append(Member(manifest, binding, current))
    return found[:8]


def _max_seconds(rt: Runtime, rows: list[Member]) -> int:
    table = rt.permission_ceiling or {}
    return min((table[m.manifest.capability_id].seconds for m in rows), default=0)


async def status(rt: Runtime, scope: Scope) -> InitialAccessStatus:
    rows = await members(rt, scope) if rt.permission_ceiling else []
    now = rt.supervisor.clock.utc()
    reason = None
    if not rt.permission_ceiling or not rows:
        reason = "INITIAL_ACCESS_NOT_CONFIGURED"
    elif any(m.binding is None for m in rows):
        reason = "BINDING_NOT_READY"
    return InitialAccessStatus(
        scope=scope,
        configured=bool(rows),
        ready=reason is None,
        max_duration_seconds=min(_max_seconds(rt, rows), 31_536_000),
        members=[
            InitialAccessCapability(
                capability_id=m.manifest.capability_id,
                summary=m.manifest.summary,
                areas=list(m.manifest.areas),
                available=_active(m.current, now),
                expires_at=m.current.expires_at if _active(m.current, now) and m.current else None,
            )
            for m in rows
        ],
        unavailable_reason=reason,
    )


def guard(
    rt: Runtime, authority: ControlAuthority, request: PlanCommand | ExecutePlanCommand
) -> None:
    rt.authorize_configuration(authority, request.scope)
    if (
        request.scope != area(request.scope)
        or request.scope.memory_mode != "work"
        or request.target != TARGET
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)


async def _workspace(rt: Runtime, scope: Scope) -> Workspace:
    found = await rt.store.repository(Workspace, PRINCIPALS[Owner.WORKSPACE], scope).get(
        scope.workspace_id
    )
    if found is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    return found


def _pins(scope: Scope, rows: list[Member]) -> list[InitialAccessMember]:
    pinned: list[InitialAccessMember] = []
    for m in rows:
        assert m.binding is not None
        pinned.append(
            InitialAccessMember(
                capability_id=m.manifest.capability_id,
                manifest_id=m.manifest.id,
                manifest_version=m.manifest.version,
                binding_id=m.binding.binding_id,
                binding_version=m.binding.version,
                permission_id=permission_id(scope, m.manifest.capability_id, m.binding.binding_id),
                permission_version=m.current.version if m.current else None,
                areas=list(m.manifest.areas),
            )
        )
    return pinned


def summary(decision: InitialAccessArguments, rows: list[Member]) -> str:
    days = decision.duration_seconds // 86400
    lines = [
        f"«{m.manifest.summary}» ({', '.join(m.manifest.areas) or 'без файловых областей'})"
        + ("; прежнее право продлевается" if m.current is not None else "")
        for m in rows
    ]
    return (
        f"Выдать агенту начальный доступ на {days} дн., уровень «автоматически»: "
        + "; ".join(lines)
        + f". Ограничения: {decision.constraints or 'нет'}. Открытые запросы этих возможностей "
        "будут закрыты как решённые. Возможность, добавленная позже, этот допуск не получает. "
        "Решение не запускает агента и не отменяет модерацию и права источников."
    )


async def preview(rt: Runtime, authority: ControlAuthority, request: PlanCommand) -> Receipt:
    if request.arguments.schema_id != SCHEMA or request.expected_version != 1:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    decision = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(decision, InitialAccessArguments):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    guard(rt, authority, request)
    rows = await members(rt, request.scope)
    if not rows:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if any(m.binding is None for m in rows):
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if decision.duration_seconds > _max_seconds(rt, rows):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    workspace = await _workspace(rt, request.scope)
    now = rt.supervisor.clock.utc()
    identifier = derived_id("initial-access-plan", request.scope, request.request_id)
    proof = InitialAccessProof(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operator_id=authority.principal_id,
        capabilities=[m.manifest.capability_id for m in rows],
        members=_pins(request.scope, rows),
        decision=decision,
        policy_version=workspace.policy_version,
        plan_expires_at=now + timedelta(minutes=PLAN_MINUTES),
        expires_at=now + rt.store.receipt_retention,
    )
    dependencies = [
        PlanDependency(resource_id=row.id, version=row.version)
        for m in rows
        for row in (m.manifest, m.binding, m.current)
        if row is not None
    ]
    plan = ConsequencePlan(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operation_type="workplace.access",
        consequences=summary(decision, rows),
        dependencies=dependencies,
        expires_at=proof.plan_expires_at,
    )
    with rt.store.fence(lambda: guard(rt, authority, request), owner=Owner.WORKSPACE):
        async with rt.store.transaction(
            PRINCIPALS[Owner.WORKSPACE], request.scope, request
        ) as unit:
            if unit.replay is not None:
                return unit.replay
            await unit.put(plan, access=rt.readers)
            await unit.put(proof)
            receipt = Receipt(
                request_id=request.request_id,
                scope=request.scope,
                status="completed",
                resource_id=plan.id,
                version=1,
            )
            await unit.complete(receipt)
            return receipt


def _unchanged(proof: InitialAccessProof, rows: list[Member], workspace: Workspace) -> bool:
    current = {m.manifest.capability_id: m for m in rows}
    if [m.manifest.capability_id for m in rows] != proof.capabilities:
        return False  # the set changed (a capability added, removed or no longer automatic)
    for pin in proof.members:
        m = current[pin.capability_id]
        if (
            m.binding is None
            or m.manifest.id != pin.manifest_id
            or m.manifest.version != pin.manifest_version
            or m.binding.binding_id != pin.binding_id
            or m.binding.version != pin.binding_version
            or (m.current.version if m.current else None) != pin.permission_version
        ):
            return False
    return workspace.policy_version == proof.policy_version


async def execute(rt: Runtime, authority: ControlAuthority, request: ExecutePlanCommand) -> Receipt:
    guard(rt, authority, request)
    owner = PRINCIPALS[Owner.WORKSPACE]
    proof = await rt.store.repository(InitialAccessProof, owner, request.scope).get(
        request.arguments.plan_id
    )
    if proof is None:
        raise ContractError(ErrorCode.EXPIRED)
    if (
        proof.operator_id != authority.principal_id
        or request.arguments.plan_version != 1
        or request.expected_version != 1
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    now = rt.supervisor.clock.utc()
    if proof.executed_by is None:
        if now >= proof.plan_expires_at:
            raise ContractError(ErrorCode.EXPIRED)
        rows = await members(rt, request.scope)
        if not _unchanged(proof, rows, await _workspace(rt, request.scope)):
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        if proof.decision.duration_seconds > _max_seconds(rt, rows):
            raise ContractError(ErrorCode.ACCESS_DENIED)  # the host ceiling was lowered
    elif proof.executed_by != request.request_id:
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    else:
        rows = await members(rt, request.scope)
    by_capability = {m.manifest.capability_id: m for m in rows}
    rights: list[tuple[Permission, int | None]] = []
    for pin in proof.members:
        current = by_capability[pin.capability_id].current
        rights.append(
            (
                record(
                    rt.registry,
                    Permission,
                    id=pin.permission_id,
                    scope=request.scope,
                    version=(current.version + 1) if current else 1,
                    created_at=current.created_at if current else now,
                    updated_at=now,
                    principal_id=HOLDER,
                    capability_id=pin.capability_id,
                    operation_type=pin.capability_id,
                    binding_id=pin.binding_id,
                    level="automatic",
                    conditions=extension(
                        rt.registry,
                        "workplace.permission-conditions",
                        {
                            "areas": list(pin.areas),
                            "constraints": proof.decision.constraints,
                            "ticket_id": None,
                            "ticket_version": None,
                        },
                    ),
                    limits=extension(
                        rt.registry,
                        "workplace.permission-limits",
                        {"duration_seconds": proof.decision.duration_seconds},
                    ),
                    policy_version=proof.policy_version,
                    author_id=authority.principal_id,
                    expires_at=now + timedelta(seconds=proof.decision.duration_seconds),
                ),
                pin.permission_version,
            )
        )
    done = proof.model_copy(
        update={"version": 2, "updated_at": now, "executed_by": request.request_id}
    )
    with rt.store.fence(lambda: guard(rt, authority, request), owner=Owner.WORKSPACE):
        async with rt.store.transaction(owner, request.scope, request) as unit:
            if unit.replay is not None:
                receipt = unit.replay
            else:
                for permission, expected in rights:  # all members or none
                    await unit.put(permission, expected_version=expected, access=rt.readers)
                await unit.put(
                    InitialAccessProof.model_validate(
                        done.model_dump(), context={"extensions": rt.registry}
                    ),
                    expected_version=1,
                )
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=proof.id,
                    version=2,
                )
                await unit.complete(receipt)
    for permission, _ in rights:
        rt.notify("connection.changed", request.scope, permission.id, permission.version)
    await close_requests(rt, authority, request, proof)
    return receipt


async def close_requests(
    rt: Runtime, authority: ControlAuthority, request: ExecutePlanCommand, proof: InitialAccessProof
) -> None:
    """Close the set's open capability requests as resolved (owner receipt, replay-safe)."""
    if rt.tickets is None:
        return
    covered = set(proof.capabilities)
    rows = await rt.store.repository(Ticket, PRINCIPALS[Owner.TICKETS], area(request.scope)).list(
        limit=200
    )
    for ticket in rows:
        if (
            ticket.kind != "capability_request"
            or ticket.request is None
            or ticket.request.capability_id not in covered
            or ticket.state in ("resolved", "closed_unfulfilled")
        ):
            continue
        command = EditCommand.model_validate(
            dict(
                type="ticket.reply",
                request_id=derived_id("initial-access-close", request.scope, ticket.id + proof.id),
                scope=area(request.scope).model_dump(mode="json"),
                target=ticket.id,
                expected_version=ticket.version,
                arguments=dict(
                    schema_id="ticket.reply-args",
                    data={
                        "ticket_id": ticket.id,
                        "body": "Выдан начальный доступ рабочего места; запрос закрыт.",
                        "outcome": "resolve",
                    },
                ),
            ),
            context={"extensions": rt.registry},
        )
        args = TicketReplyArguments.model_validate(command.arguments.data)
        try:
            receipt = await rt.tickets.reply(authority.principal_id, command, args)
        except ContractError as error:
            if error.error.code != ErrorCode.VERSION_CONFLICT:
                raise
            continue  # changed meanwhile: the operator sees it open and decides there
        rt.notify("ticket.changed", area(request.scope), ticket.id, receipt.version)
