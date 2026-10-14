"""Durable reservations within an explicitly prepared, already authorised TEST pool."""

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import CreateCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import LaboratoryScopeReservation

from .service import Runtime
from .state import PRINCIPALS


def identifier(parent: Scope, child: Scope) -> str:
    return child.memory_space_id


async def prepare(runtime: Runtime, authority: ControlAuthority, command: CreateCommand) -> Receipt:
    if runtime.lab is None or runtime.reports is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    # No factory call, grants, migrations, copying a profile or model IO here.
    async with runtime._lock:
        runtime.authorize_configuration(authority, command.scope)
        await runtime.policy.permit(runtime.supervisor.snapshot.scope, "laboratory")
        store = runtime.store
        async with store.transaction(PRINCIPALS[Owner.LABORATORY], command.scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            for child in runtime.scopes[:8]:
                if (
                    child.memory_mode != "test"
                    or child.agent_id is not None
                    or child.workspace_id != command.scope.workspace_id
                    or child.persona_id != command.scope.persona_id
                    or child.partition not in authority.partitions
                ):
                    continue
                key = identifier(command.scope, child)
                previous = await store.repository(
                    LaboratoryScopeReservation, PRINCIPALS[Owner.LABORATORY], command.scope
                ).get(key)
                if previous is not None or not await store.empty_test_scope(child):
                    continue
                runtime.authorize_configuration(authority, command.scope)
                now = runtime.supervisor.clock.utc()
                reservation = LaboratoryScopeReservation(
                    id=key,
                    scope=command.scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    request_id=command.request_id,
                    lab_scope=child,
                )
                await runtime.policy.permit(runtime.supervisor.snapshot.scope, "laboratory")
                await unit.put(reservation)
                receipt = Receipt(
                    request_id=command.request_id,
                    scope=command.scope,
                    status="completed",
                    resource_id=child.memory_space_id,
                    version=1,
                )
                await unit.complete(receipt)
                return receipt
            raise ContractError(ErrorCode.RESOURCE_BUSY)
