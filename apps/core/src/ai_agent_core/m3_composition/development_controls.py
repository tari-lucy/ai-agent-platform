"""Explicit operator development controls over PERSONA/GM and reviewed rollback plans."""

from datetime import timedelta
from typing import TYPE_CHECKING

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key, stamp, visible
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.development import DevelopmentController, MemoryEvidence
from ai_agent_application.dialogue import ControlSnapshot
from ai_agent_application.gamemaster import Dice, GameMaster
from ai_agent_application.gamemaster.rules import screen
from ai_agent_application.memory import MemoryService
from ai_agent_application.memory.service import derived_id
from ai_agent_application.moderation.local import secret_like
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import (
    CommandBase,
    ConsequencePlan,
    EditCommand,
    ExecutePlanCommand,
    PlanCommand,
    PlanDependency,
    Receipt,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import VersionedRecord
from ai_agent_contracts.memory import Claim, Source, Summary
from ai_agent_contracts.panel import (
    ChangeDecisionArguments,
    ChangeInterpretationArguments,
    ResourceArguments,
    RollbackProof,
)
from ai_agent_contracts.persona import (
    CharacterState,
    GmRuntimeState,
    Opportunity,
    Profile,
    StateChange,
    StoryArc,
)
from ai_agent_contracts.states import ChangeState, ErrorCode
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from .owner_operations import OwnerOperations
from .state import PRINCIPALS, rules_of

if TYPE_CHECKING:
    from .service import Runtime

MODELS: dict[str, type[VersionedRecord]] = {
    m.__name__: m
    for m in (
        Source, Claim, Summary, StateChange, CharacterState, Profile,
        GmRuntimeState, StoryArc, Opportunity,
    )
}
OWNERS = {
    "Source": Owner.MEMORY,
    "Claim": Owner.MEMORY,
    "Summary": Owner.MEMORY,
    "Connection": Owner.WORKSPACE,
    "Tariff": Owner.LLM,
    "StateChange": Owner.PERSONA,
    "CharacterState": Owner.PERSONA,
    "Profile": Owner.PERSONA,
    "GmRuntimeState": Owner.GM,
    "Opportunity": Owner.GM,
    "StoryArc": Owner.GM,
}


class Pins:
    def __init__(
        self, rt: Runtime, authority: ControlAuthority, request: CommandBase, owner: Owner
    ):
        self.rt, self.authority, self.request, self.owner = rt, authority, request, owner
        self.epoch = rt.supervisor.snapshot.control_epoch
        self.records: dict[str, VersionedRecord] = {}
        self.entered = False

    async def read[T: VersionedRecord](self, model: type[T], identifier: str) -> T:
        row = await self.rt.store.repository(
            model, Principal(self.authority.principal_id, "operator"), self.request.scope
        ).get(identifier, purpose="api_transfer")
        if row is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.records[model.__name__ + ":" + row.id] = row
        return row

    def fence(self) -> None:
        self.rt.authorize_configuration(self.authority, self.request.scope)
        if self.rt.supervisor.snapshot.control_epoch != self.epoch:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    async def check(self, connection: AsyncConnection) -> None:
        self.fence()
        for key, record in self.records.items():
            kind = key.split(":", 1)[0]
            pin = not self.entered or OWNERS[kind] != self.owner
            row = db.records
            valid = await connection.scalar(
                select(row.c.id).where(
                    row.c.scope_id == scope_key(record.scope),
                    row.c.kind == kind,
                    row.c.id == record.id,
                    row.c.version == record.version if pin else true(),
                    visible(
                        row,
                        row.c.version,
                        Principal(self.authority.principal_id, "operator"),
                        "api_transfer",
                        stamp(self.rt.store.clock()),
                    ),
                )
            )
            if valid is None:
                raise ContractError(ErrorCode.STALE_CONTEXT)
        self.entered = True
        self.fence()

    async def context(self) -> None:
        scope = self.request.scope
        await self.read(Profile, scope.persona_id)
        await self.read(CharacterState, "state:" + scope.persona_id)
        await self.read(GmRuntimeState, "gm-runtime")

    async def evidence(self, change: StateChange) -> None:
        for ref in change.evidence:
            source = await self.read(Source, ref.source_id)
            if source.version != ref.version or ref.scope.partition != self.request.scope.partition:
                raise ContractError(ErrorCode.STALE_CONTEXT)


def controller(rt: Runtime) -> DevelopmentController:
    memory = MemoryService(
        rt.store, PRINCIPALS[Owner.MEMORY], rt.registry, clock=rt.supervisor.clock.utc
    )

    def control() -> ControlSnapshot:
        run = rt.supervisor.snapshot
        return ControlSnapshot(run.state, run.activity, run.autonomy, False)

    return DevelopmentController(
        rt.store,
        PRINCIPALS[Owner.PERSONA],
        rt.registry,
        MemoryEvidence(memory),
        control,
        clock=rt.supervisor.clock.utc,
        reader_access=rt.readers,
    )


async def preview(rt: Runtime, authority: ControlAuthority, request: PlanCommand) -> Receipt:
    if (
        request.type != "change.rollback.preview"
        or request.arguments.schema_id != "change.rollback-args"
    ):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    pins = Pins(rt, authority, request, Owner.WORKSPACE)
    change = await pins.read(StateChange, request.target)
    if change.version != request.expected_version:
        raise ContractError(ErrorCode.VERSION_CONFLICT)
    await pins.context()
    domain = await controller(rt).preview_rollback(request.scope, change.id)
    for identifier in (*domain.chain, *domain.invalidated_pending):
        row = await pins.read(StateChange, identifier)
        await pins.evidence(row)
    if len(pins.records) > 200:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    now = rt.supervisor.clock.utc()
    identifier = derived_id("rollback-plan", request.scope, request.request_id)
    plan = ConsequencePlan(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        operation_type="change.rollback",
        expires_at=now + timedelta(minutes=5),
        consequences=(
            f"Свойство {domain.property}: "
            f"{domain.current_value!r} → {domain.restore_value!r}. "
            f"Компенсирующих версий: {len(domain.chain)}. "
            f"Ожидающих предложений для пересмотра: {len(domain.invalidated_pending)}. "
            "Опыт, источники и отправленные реплики сохраняются."
        ),
        dependencies=[
            PlanDependency(resource_id=row.id, version=row.version) for row in pins.records.values()
        ],
    )
    proof = RollbackProof(
        id=identifier,
        scope=request.scope,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=plan.expires_at,
        operator_id=authority.principal_id,
        change_id=change.id,
        chain_ids=list(domain.chain),
        digest=domain.digest,
        pins={key: row.version for key, row in pins.records.items()},
    )
    with (
        rt.store.fence(pins.fence, owner=Owner.WORKSPACE),
        rt.store.authorize_writes(pins.check, owners=frozenset({Owner.WORKSPACE})),
    ):
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
                version=plan.version,
            )
            await unit.complete(receipt)
            return receipt


async def mutate(
    rt: Runtime, authority: ControlAuthority, request: EditCommand | ExecutePlanCommand
) -> Receipt:
    is_rollback = isinstance(request, ExecutePlanCommand)
    if is_rollback and request.type != "change.rollback.execute":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    owner = Owner.GM if request.type in ("opportunity.cancel", "arc.pause") else Owner.PERSONA
    pins = Pins(rt, authority, request, owner)
    ops = OwnerOperations(rt)
    known = await ops.known(authority, request)
    if known is not None and known.receipt is not None:
        return known.receipt
    proof = None
    if is_rollback and known is None:
        assert isinstance(request, ExecutePlanCommand)
        proof = await rt.store.repository(
            RollbackProof, PRINCIPALS[Owner.WORKSPACE], request.scope
        ).get(request.arguments.plan_id)
        if proof is None:
            raise ContractError(ErrorCode.EXPIRED)
        if (
            proof.change_id != request.target
            or proof.operator_id != authority.principal_id
            or proof.version != request.arguments.plan_version
            or request.expected_version != proof.version
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if known is None:
            if rt.supervisor.clock.utc() >= proof.expires_at:
                raise ContractError(ErrorCode.EXPIRED)
            for key, version in proof.pins.items():
                kind, identifier = key.split(":", 1)
                row = await pins.read(MODELS[kind], identifier)
                if row.version != version:
                    raise ContractError(ErrorCode.STALE_CONTEXT)
        args = None
    elif is_rollback:
        args = None
    else:
        assert isinstance(request, EditCommand)
        expected = {
            "change.decision": "change.decision-args",
            "change.interpretation": "change.interpretation-args",
            "opportunity.cancel": "opportunity.cancel-args",
            "arc.pause": "arc.pause-args",
        }.get(request.type)
        if request.arguments.schema_id != expected:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        args = rt.registry.validate(
            request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
        )
    model = (
        Opportunity
        if request.type == "opportunity.cancel"
        else StoryArc
        if request.type == "arc.pause"
        else StateChange
    )
    target = await pins.read(model, request.target)
    if known is None and not is_rollback and target.version != request.expected_version:
        raise ContractError(ErrorCode.VERSION_CONFLICT)
    if known is None:
        await pins.context()
        if isinstance(target, StateChange):
            await pins.evidence(target)
            if request.type == "change.decision" and target.status not in (
                ChangeState.READY,
                ChangeState.AWAITING_APPROVAL,
            ):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            if isinstance(
                args, (ChangeDecisionArguments, ChangeInterpretationArguments)
            ) and secret_like(args.reason):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if isinstance(args, ChangeInterpretationArguments) and secret_like(args.interpretation):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if isinstance(args, ChangeDecisionArguments) and args.decision == "accept":
                gm = await rt.state.gm(request.scope)
                verdict = await screen(
                    rules_of(gm, rt.registry),
                    target.interpretation + "\n" + target.expected_manifestation,
                    None,
                )
                if not verdict.allowed:
                    raise ContractError(ErrorCode.ACCESS_DENIED)

    async def recover(operation_id: str) -> list[VersionedRecord] | None:
        if is_rollback:
            records: list[VersionedRecord] = []
            identifiers = (
                known.expected_result_ids
                if known is not None
                else [
                    derived_id("chg", request.scope, "rollback", original_id)
                    for original_id in proof.chain_ids
                ]
                if proof
                else []
            )
            if not identifiers:
                return None
            for identifier in identifiers:
                row = await rt.store.repository(
                    StateChange, PRINCIPALS[Owner.PERSONA], request.scope
                ).get(identifier)
                if row is None or row.operator_decision_id != operation_id:
                    return None
                records.append(row)
            return records
        current = await rt.store.repository(model, PRINCIPALS[owner], request.scope).get(
            request.target
        )
        if isinstance(current, StateChange) and current.operator_decision_id == operation_id:
            return [current]
        desired = (
            isinstance(current, StoryArc)
            and current.status == "paused"
            or isinstance(current, Opportunity)
            and current.status.value == "CANCELLED"
        )
        if desired and current is not None and current.version == request.expected_version + 1:
            return [current]
        return None

    async def perform(operation_id: str) -> list[VersionedRecord]:
        with (
            rt.store.fence(pins.fence, owner=owner),
            rt.store.authorize_writes(pins.check, owners=frozenset({owner})),
        ):
            service = controller(rt)
            if is_rollback:
                assert proof is not None
                return list(
                    await service.rollback(
                        request.scope, request.target, operation_id, proof.digest
                    )
                )
            if isinstance(args, ChangeDecisionArguments):
                return [
                    await service.decide(
                        request.scope,
                        request.target,
                        args.decision == "accept",
                        operation_id,
                        reason=args.reason,
                    )
                ]
            if isinstance(args, ChangeInterpretationArguments):
                return [
                    await service.correct_interpretation(
                        request.scope,
                        request.target,
                        args.interpretation,
                        args.reason,
                        operation_id,
                    )
                ]
            assert isinstance(args, ResourceArguments)
            gm = await rt.state.gm(request.scope)
            master = GameMaster(
                rt.store,
                PRINCIPALS[Owner.GM],
                rt.registry,
                MemoryService(rt.store, PRINCIPALS[Owner.MEMORY], rt.registry),
                rules_of(gm, rt.registry),
                rt.families,
                Dice(operation_id),
                clock=rt.supervisor.clock.utc,
                reader_access=rt.readers,
            )
            if request.type == "opportunity.cancel":
                return [await master.cancel(request.scope, request.target)]
            return [await master.set_arc_status(request.scope, request.target, "paused")]

    result = await ops.execute(
        authority,
        request,
        "gm" if owner is Owner.GM else "persona",
        perform,
        recover,
        expected_result_ids=[
            derived_id("chg", request.scope, "rollback", original_id)
            for original_id in proof.chain_ids
        ]
        if proof
        else None,
    )
    rt.notify(
        "arc.changed" if owner is Owner.GM else "change.changed", request.scope, request.target
    )
    return result
