"""Operator settings through the existing host-config owner and LLM usage ledger."""

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.storage import Owner
from ai_agent_application.usage import TariffDraft, UsageLedger, tariff_record_id
from ai_agent_contracts.api import CreateCommand, EditCommand, Receipt
from ai_agent_contracts.codec import ContractError, command_fingerprint
from ai_agent_contracts.common import Scope, VersionedRecord
from ai_agent_contracts.llm import Connection, Tariff
from ai_agent_contracts.panel import (
    ApiLimitArguments,
    ApiLimitChange,
    ApiLimits,
    TariffCreateArguments,
)
from ai_agent_contracts.states import ErrorCode, RunState

from .development_controls import Pins
from .owner_operations import OwnerOperations
from .state import PRINCIPALS, put

if TYPE_CHECKING:
    from .service import Runtime


class BudgetControls(Protocol):
    async def read(self, scope: Scope) -> ApiLimits: ...
    async def apply(
        self, authority: ControlAuthority, request: EditCommand, limit: int
    ) -> ApiLimitChange: ...
    async def result(
        self, authority: ControlAuthority, request: EditCommand
    ) -> ApiLimitChange | None: ...


async def limits(rt: Runtime, authority: ControlAuthority, request: EditCommand) -> Receipt:
    port = rt.budget_controls
    if port is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if request.target != "api-limits" or request.arguments.schema_id != "llm.limits-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(args, ApiLimitArguments):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    def guard() -> None:
        rt.authorize_configuration(authority, request.scope)
        if rt.supervisor.snapshot.state is not RunState.STOPPED:
            raise ContractError(ErrorCode.RESOURCE_BUSY)

    guard()
    ops = OwnerOperations(rt)
    known = await ops.known(authority, request)
    if known is not None and known.receipt is not None:
        return known.receipt
    if known is None:
        current = await port.read(request.scope)
        if current.version != request.expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, current.version)
        if (
            not current.minimum_limit <= args.attempt_limit <= 1_000_000
            or args.attempt_limit == current.attempt_limit
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    async def save(row: ApiLimitChange) -> list[VersionedRecord]:
        existing = await rt.store.repository(
            ApiLimitChange, PRINCIPALS[Owner.WORKSPACE], request.scope
        ).get(row.id)
        if existing is not None:
            if existing != row:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            return [existing]
        await put(
            rt.store,
            rt.registry,
            Owner.WORKSPACE,
            row.scope,
            "budget-result-" + row.id,
            row,
            None,
            rt.readers,
        )
        return [row]

    async def perform(_identifier: str) -> list[VersionedRecord]:
        return await save(await port.apply(authority, request, args.attempt_limit))

    async def recover(_identifier: str) -> list[VersionedRecord] | None:
        from ai_agent_application.memory import derived_id

        identifier = derived_id("budget-change", request.scope, request.request_id)
        row = await rt.store.repository(
            ApiLimitChange, PRINCIPALS[Owner.WORKSPACE], request.scope
        ).get(identifier)
        if row is not None:
            return (
                [row]
                if row.operator_id == authority.principal_id
                and row.fingerprint == command_fingerprint(request)
                else None
            )
        row = await port.result(authority, request)
        return None if row is None else await save(row)

    with rt.store.fence(guard, owner=Owner.WORKSPACE):
        return await ops.execute(authority, request, "workspace", perform, recover)


async def tariff(rt: Runtime, authority: ControlAuthority, request: CreateCommand) -> Receipt:
    if request.arguments.schema_id != "tariff.create-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(args, TariffCreateArguments) or request.target != args.connection_id:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if args.effective_at > rt.supervisor.clock.utc() + timedelta(days=366):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    pins = Pins(rt, authority, request, Owner.LLM)
    await pins.read(Connection, args.connection_id)
    identifier = tariff_record_id(request.scope, args.connection_id, args.model_id)
    current = await rt.store.repository(Tariff, PRINCIPALS[Owner.LLM], request.scope).get(
        identifier
    )
    if current is not None:
        await pins.read(Tariff, identifier)
    with (
        rt.store.fence(pins.fence, owner=Owner.LLM),
        rt.store.authorize_writes(pins.check, owners=frozenset({Owner.LLM})),
    ):
        return await UsageLedger(
            rt.store,
            PRINCIPALS[Owner.LLM],
            rt.registry,
            clock=rt.supervisor.clock.utc,
            reader_access=rt.readers,
        ).create_tariff(
            request,
            TariffDraft(**args.model_dump(exclude={"previous_version"})),
            args.previous_version,
            operator_id=authority.principal_id,
        )
