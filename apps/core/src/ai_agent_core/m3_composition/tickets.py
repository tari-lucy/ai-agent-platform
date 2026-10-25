"""Operator ticket commands through the tickets owner; allowed while the agent is STOPPED.

A reply is stored and the turn moves to the agent, but nothing starts: processing waits for an
explicit START. Ticket text never grants a right (permission decisions are separate commands).
"""

from typing import TYPE_CHECKING

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_workplace import TicketReadArguments, TicketReplyArguments
from ai_agent_contracts.api import EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

if TYPE_CHECKING:
    from .service import Runtime


async def mutate(rt: Runtime, authority: ControlAuthority, request: EditCommand) -> Receipt:
    service = rt.tickets
    if service is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if request.arguments.schema_id != request.type + "-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )

    def current() -> None:
        # Session/partition are rechecked inside the owner's write transaction.
        rt.authorize_configuration(authority, request.scope)

    with rt.store.fence(current, owner=Owner.TICKETS):
        current()
        if request.type == "ticket.reply" and isinstance(args, TicketReplyArguments):
            receipt = await service.reply(authority.principal_id, request, args)
        elif request.type == "ticket.read" and isinstance(args, TicketReadArguments):
            receipt = await service.mark_read(request, args)
        else:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if receipt.resource_id is not None:
        rt.notify("ticket.changed", area(request.scope), receipt.resource_id, receipt.version)
    if request.type == "ticket.reply":
        rt.wake_activity(request.scope)
    return receipt
