"""Explicit operator closure of an exact promise through the existing social owner."""

from typing import TYPE_CHECKING

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import SourceDraft
from ai_agent_application.memory.service import derived_id
from ai_agent_application.social import SocialService
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Promise
from ai_agent_contracts.panel import PromiseCloseArguments
from ai_agent_contracts.states import ErrorCode, Provenance
from sqlalchemy.ext.asyncio import AsyncConnection

from .development_controls import Pins
from .state import PRINCIPALS, command

if TYPE_CHECKING:
    from .service import Runtime


async def close(rt: Runtime, authority: ControlAuthority, request: EditCommand) -> Receipt:
    if request.arguments.schema_id != "promise.close-args":
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    args = rt.registry.validate(
        request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
    )
    if not isinstance(args, PromiseCloseArguments) or args.promise_id != request.target:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    pins = Pins(rt, authority, request, Owner.SOCIAL)
    promise = await pins.read(Promise, request.target)  # the operator must see it now
    if promise.version != request.expected_version:
        # A lost HTTP response must reconcile the same command from the owner's receipt,
        # before rejecting its now-old version. The transaction checks payload and authority.
        with rt.store.fence(pins.fence, owner=Owner.SOCIAL):
            async with rt.store.transaction(
                PRINCIPALS[Owner.SOCIAL], request.scope, request
            ) as unit:
                if unit.replay is not None:
                    return unit.replay
                raise ContractError(ErrorCode.VERSION_CONFLICT, request.request_id, promise.version)
    if promise.status not in ("open", "unknown"):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS, request.request_id)
    from .dialogue import memory

    evidence: SourceRef | None = None
    owners = frozenset({Owner.SOCIAL, Owner.MEMORY})

    async def current(connection: AsyncConnection) -> None:
        # The operator session and control epoch are rechecked in each write transaction;
        # the promise itself is pinned by the social owner's version CAS.
        pins.fence()

    with (
        rt.store.fence(pins.fence, owner=Owner.SOCIAL),
        rt.store.fence(pins.fence, owner=Owner.MEMORY),
        rt.store.authorize_writes(current, owners=owners),
    ):
        pins.fence()
        if args.outcome == "fulfilled":
            assert args.closure_result is not None
            # The operator's own statement grounds fulfilment; the reply never proves itself.
            made = await memory(rt).register_source(
                command(
                    request.scope, derived_id("promise-evidence", request.scope, request.request_id)
                ),
                SourceDraft(
                    provenance=Provenance.SIMULATION
                    if request.scope.memory_mode == "test"
                    else Provenance.OPERATOR_INPUT,
                    source_type="promise-closure",
                    adapter_id="panel",
                    adapter_version="1",
                    occurred_at=rt.supervisor.clock.utc(),
                    content=args.closure_result,
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                    external_id=request.request_id,
                ),
            )
            evidence = SourceRef(
                source_id=made.source_id,
                version=made.version,
                scope=request.scope,
                availability="available",
            )
        social = SocialService(
            rt.store,
            PRINCIPALS[Owner.SOCIAL],
            rt.registry,
            clock=rt.supervisor.clock.utc,
            reader_access=rt.readers,
        )
        closed = await social.close_promise(
            request,
            promise.id,
            promise.version,
            args.outcome,
            args.closure_result,
            evidence,
        )
    return Receipt(
        request_id=request.request_id,
        scope=request.scope,
        status="completed",
        resource_id=closed.id,
        version=closed.version,
    )
