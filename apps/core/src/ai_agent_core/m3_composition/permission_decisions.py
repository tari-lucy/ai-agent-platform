"""Operator permission decisions for workplace capabilities (AW-01): preview, then execute.

Only an authenticated operator session reaches this module (panel commands); agent, worker and
runner credentials have no command that maps here. The workspace owner writes the Permission.
Every pinned version (ticket, manifest, binding, permission, workspace policy) and the host
ceiling are checked at preview and again inside the execute decision. A grant does not make a
binding ready, does not start a STOPPED agent and does not bypass moderation or source rights.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import derived_id
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    PermissionDecisionArguments,
    PermissionDecisionProof,
    Ticket,
)
from ai_agent_contracts.api import (
    ConsequencePlan,
    ExecutePlanCommand,
    Permission,
    PlanCommand,
    PlanDependency,
    Receipt,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import Workspace

from .state import PRINCIPALS, extension, record

if TYPE_CHECKING:
    from .service import Runtime

Level = Literal["prepare_only", "operator_decision", "automatic"]
RANK: dict[str, int] = {"prepare_only": 1, "operator_decision": 2, "automatic": 3}
HOLDER = "workplace-agent"
PLAN_MINUTES = 5


@dataclass(frozen=True)
class Ceiling:
    level: Level
    seconds: int | None  # None: any term, including until revoked (D78)


def within(duration: int | None, limit: int | None) -> bool:
    """A term inside the host ceiling; until-revoked (None) needs a ceiling without a limit."""
    if limit is None:
        return True
    return duration is not None and duration <= limit


def until(now: datetime, duration: int | None) -> datetime:
    from ai_agent_contracts.workplace_access import STANDING_UNTIL

    return STANDING_UNTIL if duration is None else now + timedelta(seconds=duration)


def term_text(duration: int | None) -> str:
    return "до отзыва" if duration is None else f"на {duration // 86400} дн."


@dataclass(frozen=True)
class Pinned:
    manifest: CapabilityManifest
    binding: CapabilityBinding
    workspace: Workspace
    ticket: Ticket | None
    current: Permission | None


def permission_id(decision: PermissionDecisionArguments, scope: Scope) -> str:
    return derived_id(
        "workplace-permission", area(scope), decision.capability_id + "|" + decision.binding_id
    )


def ceiling(rt: Runtime, decision: PermissionDecisionArguments) -> Ceiling:
    table = rt.permission_ceiling
    if table is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    entry = table.get(decision.capability_id)
    if entry is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    return entry


async def pinned(rt: Runtime, decision: PermissionDecisionArguments, scope: Scope) -> Pinned:
    workspace_owner = PRINCIPALS[Owner.WORKSPACE]
    manifests = [
        row
        for row in await rt.store.repository(CapabilityManifest, workspace_owner, scope).list(
            limit=200
        )
        if row.capability_id == decision.capability_id
    ]
    binding = await rt.store.repository(CapabilityBinding, workspace_owner, scope).get(
        decision.binding_id
    )
    workspace = await rt.store.repository(Workspace, workspace_owner, scope).get(scope.workspace_id)
    if (
        len(manifests) != 1
        or binding is None
        or binding.capability_id != decision.capability_id
        or binding.state == "revoked"
        or workspace is None
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    manifest = manifests[0]
    if binding.capability_version != manifest.capability_version:
        raise ContractError(ErrorCode.STALE_CONTEXT)
    ticket = None
    if decision.ticket_id is not None:
        if rt.tickets is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        ticket = await rt.tickets.get(scope, decision.ticket_id)
        if ticket is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    current = await rt.store.repository(Permission, workspace_owner, scope).get(
        permission_id(decision, scope)
    )
    return Pinned(manifest, binding, workspace, ticket, current)


def check(rt: Runtime, decision: PermissionDecisionArguments, pins: Pinned) -> None:
    """The structural maximum: the host ceiling, the manifest's areas and effect, the ticket.

    Revocation is always allowed for an existing right, also outside the current ceiling.
    """
    if decision.action == "grant":
        limit = ceiling(rt, decision)
        assert decision.level is not None
        ticket = pins.ticket
        if (
            ticket is None
            or ticket.kind != "capability_request"
            or ticket.request is None
            or ticket.request.capability_id != decision.capability_id
            or ticket.state in ("resolved", "closed_unfulfilled")
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if ticket.version != decision.ticket_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, ticket.version)
        if (
            RANK[decision.level] > RANK[limit.level]
            or not within(decision.duration_seconds, limit.seconds)
            or not set(decision.areas) <= set(pins.manifest.areas)
            # A public effect is never automatic, whatever the configured ceiling says.
            or (pins.manifest.effect == "public_output" and decision.level == "automatic")
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        current = pins.current
        if (
            current is not None
            and decision.permission_version is not None
            and (current.version != decision.permission_version)
        ):
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, current.version)
    else:
        current = pins.current
        if current is None or current.revoked_at is not None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if current.version != decision.permission_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, current.version)


def guard(
    rt: Runtime, authority: ControlAuthority, request: PlanCommand | ExecutePlanCommand
) -> None:
    rt.authorize_configuration(authority, request.scope)
    if request.scope != area(request.scope) or request.scope.memory_mode != "work":
        raise ContractError(ErrorCode.ACCESS_DENIED)


def summary(decision: PermissionDecisionArguments, pins: Pinned) -> str:
    if decision.action == "revoke":
        return (
            f"Отозвать право на «{decision.capability_id}» (версия {decision.permission_version}). "
            "Задачи, зависящие от него, остановятся на следующей проверке; уже начатый внешний "
            "эффект показывается честно, поздний результат не сохраняется."
        )
    term = term_text(decision.duration_seconds)
    areas = ", ".join(decision.areas) or "без файловых областей"
    binding = (
        "подключение проверено"
        if pins.binding.state == "ready"
        else "подключение ещё не готово: тикет останется в работе до проверки"
    )
    return (
        f"Выдать агенту право «{decision.capability_id}» уровня {decision.level} {term} "
        f"({areas}). Ограничения: {decision.constraints or 'нет'}. Основание — обращение "
        f"{decision.ticket_id} версии {decision.ticket_version}; {binding}. Право не запускает "
        "агента и не отменяет модерацию и права источников."
    )


async def preview(rt: Runtime, authority: ControlAuthority, request: PlanCommand) -> Receipt:
    if request.arguments.schema_id != "permission.decision-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    decision = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if (
        not isinstance(decision, PermissionDecisionArguments)
        or request.target != decision.capability_id
    ):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    guard(rt, authority, request)
    seen = decision.ticket_version if decision.action == "grant" else decision.permission_version
    if request.expected_version != seen:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    pins = await pinned(rt, decision, request.scope)
    check(rt, decision, pins)
    now = rt.supervisor.clock.utc()
    identifier = derived_id("permission-plan", request.scope, request.request_id)
    proof = PermissionDecisionProof(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operator_id=authority.principal_id,
        decision=decision,
        permission_id=permission_id(decision, request.scope),
        permission_version=pins.current.version if pins.current else None,
        capability_id=decision.capability_id,
        manifest_version=pins.manifest.version,
        binding_id=pins.binding.id,
        binding_version=pins.binding.version,
        ticket_id=pins.ticket.id if pins.ticket else None,
        ticket_version=pins.ticket.version if pins.ticket else None,
        policy_version=pins.workspace.policy_version,
        plan_expires_at=now + timedelta(minutes=PLAN_MINUTES),
        expires_at=now + rt.store.receipt_retention,
    )
    pinned_rows = (pins.manifest, pins.binding, pins.ticket, pins.current)
    dependencies = [
        PlanDependency(resource_id=row.id, version=row.version)
        for row in pinned_rows
        if row is not None
    ]
    plan = ConsequencePlan(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operation_type="permission.decision",
        consequences=summary(decision, pins),
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


async def execute(rt: Runtime, authority: ControlAuthority, request: ExecutePlanCommand) -> Receipt:
    guard(rt, authority, request)
    owner = PRINCIPALS[Owner.WORKSPACE]
    proof = await rt.store.repository(PermissionDecisionProof, owner, request.scope).get(
        request.arguments.plan_id
    )
    if proof is None:
        raise ContractError(ErrorCode.EXPIRED)
    decision = proof.decision
    if (
        proof.operator_id != authority.principal_id
        or request.target != proof.capability_id
        or request.arguments.plan_version != 1
        or request.expected_version != 1
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    now = rt.supervisor.clock.utc()
    if proof.executed_by is None:
        if now >= proof.plan_expires_at:
            raise ContractError(ErrorCode.EXPIRED)
        pins = await pinned(rt, decision, request.scope)
        check(rt, decision, pins)
        if (
            pins.manifest.version != proof.manifest_version
            or pins.binding.version != proof.binding_version
            or pins.workspace.policy_version != proof.policy_version
            or (pins.current.version if pins.current else None) != proof.permission_version
            or (pins.ticket.version if pins.ticket else None) != proof.ticket_version
        ):
            raise ContractError(ErrorCode.VERSION_CONFLICT)
    elif proof.executed_by != request.request_id:
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    else:
        pins = await pinned(rt, decision, request.scope)
    current = pins.current
    if decision.action == "grant":
        assert decision.level is not None
        version = current.version + 1 if current else 1
        permission = record(
            rt.registry,
            Permission,
            id=proof.permission_id,
            scope=request.scope,
            version=version,
            created_at=current.created_at if current else now,
            updated_at=now,
            principal_id=HOLDER,
            capability_id=decision.capability_id,
            operation_type=decision.capability_id,
            binding_id=decision.binding_id,
            level=decision.level,
            conditions=extension(
                rt.registry,
                "workplace.permission-conditions",
                {
                    "areas": list(decision.areas),
                    "constraints": decision.constraints,
                    "ticket_id": decision.ticket_id,
                    "ticket_version": decision.ticket_version,
                },
            ),
            limits=extension(
                rt.registry,
                "workplace.permission-limits",
                {"duration_seconds": decision.duration_seconds},
            ),
            policy_version=proof.policy_version,
            author_id=authority.principal_id,
            expires_at=until(now, decision.duration_seconds),
        )
    else:
        assert current is not None
        permission = current.model_copy(
            update={"version": current.version + 1, "updated_at": now, "revoked_at": now}
        )
        permission = Permission.model_validate(
            permission.model_dump(), context={"extensions": rt.registry}
        )
    done = proof.model_copy(
        update={"version": 2, "updated_at": now, "executed_by": request.request_id}
    )
    with rt.store.fence(lambda: guard(rt, authority, request), owner=Owner.WORKSPACE):
        async with rt.store.transaction(owner, request.scope, request) as unit:
            if unit.replay is not None:
                receipt = unit.replay
            else:
                expected = proof.permission_version
                await unit.put(permission, expected_version=expected, access=rt.readers)
                await unit.put(
                    PermissionDecisionProof.model_validate(
                        done.model_dump(), context={"extensions": rt.registry}
                    ),
                    expected_version=1,
                )
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=permission.id,
                    version=permission.version,
                )
                await unit.complete(receipt)
    rt.notify("connection.changed", request.scope, permission.id, receipt.version)
    if proof.ticket_id is not None and rt.tickets is not None:
        # A receipt on the ticket; the turn is unchanged and the binding readiness stays visible.
        ready = pins.binding.state == "ready"
        text = (
            f"Оператор выдал право «{decision.capability_id}» уровня {decision.level} "
            f"до {permission.expires_at:%Y-%m-%d}. "
            + ("Подключение проверено." if ready else "Подключение ещё не готово.")
            if decision.action == "grant"
            else f"Оператор отозвал право «{decision.capability_id}»."
        )
        noted = await rt.tickets.note(
            derived_id("permission-note", request.scope, request.request_id),
            request.scope,
            proof.ticket_id,
            text,
        )
        rt.notify("ticket.changed", request.scope, proof.ticket_id, noted.version)
    rt.wake_activity(request.scope)
    return receipt
