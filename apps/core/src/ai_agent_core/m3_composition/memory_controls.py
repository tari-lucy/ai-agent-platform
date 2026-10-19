"""Explicit operator corrections through the existing memory owner and transaction receipts."""

from typing import TYPE_CHECKING

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import MemoryService
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Claim, Source, Summary
from ai_agent_contracts.panel import MemoryCorrectArguments
from ai_agent_contracts.states import ErrorCode, RunState

from .development_controls import Pins
from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime


async def correct(rt: Runtime, authority: ControlAuthority, request: EditCommand) -> Receipt:
    if request.arguments.schema_id != "memory.correct-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(args, MemoryCorrectArguments) or args.record_id != request.target:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    pins = Pins(rt, authority, request, Owner.MEMORY)
    target: Claim | Summary
    if args.kind == "claim":
        target = await pins.read(Claim, request.target)
    else:
        target = await pins.read(Summary, request.target)
    for ref in target.sources:
        source = await pins.read(Source, ref.source_id)
        if (
            ref.scope.partition != request.scope.partition
            or ref.availability != "available"
            or source.version != ref.version
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)

    def stopped() -> None:
        pins.fence()
        # STOP revokes pending contexts and paid work before an operator changes memory.
        if rt.supervisor.snapshot.state is not RunState.STOPPED:
            raise ContractError(ErrorCode.RESOURCE_BUSY)

    with (
        rt.store.fence(stopped, owner=Owner.MEMORY),
        rt.store.authorize_writes(pins.check, owners=frozenset({Owner.MEMORY})),
    ):
        stopped()
        return await MemoryService(
            rt.store, PRINCIPALS[Owner.MEMORY], rt.registry, clock=rt.supervisor.clock.utc
        ).correct(request, authority.principal_id)
