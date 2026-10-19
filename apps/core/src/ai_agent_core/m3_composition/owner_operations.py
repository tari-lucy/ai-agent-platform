"""Workspace-owned operator request receipts; target state stays with its existing owner."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.codec import ContractError, command_fingerprint
from ai_agent_contracts.common import VersionedRecord
from ai_agent_contracts.panel import OwnerOperation
from ai_agent_contracts.states import ErrorCode

from .state import PRINCIPALS, put

if TYPE_CHECKING:
    from .service import Runtime


class OwnerOperations:
    def __init__(self, runtime: Runtime):
        self.rt = runtime

    async def known(
        self, authority: ControlAuthority, request: CommandBase
    ) -> OwnerOperation | None:
        row = await self.rt.store.repository(
            OwnerOperation, PRINCIPALS[Owner.WORKSPACE], request.scope
        ).get(derived_id("owner-op", request.scope, request.request_id))
        if row is None:
            return None
        if (
            row.fingerprint != command_fingerprint(request)
            or row.operator_id != authority.principal_id
        ):
            raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
        allowed = await self.rt.store.repository(
            OwnerOperation, Principal(authority.principal_id, "operator"), request.scope
        ).get(row.id, purpose="api_transfer")
        if allowed is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return row

    async def execute(
        self,
        authority: ControlAuthority,
        request: CommandBase,
        owner: Literal["persona", "gm", "memory", "llm", "workspace"],
        perform: Callable[[str], Awaitable[list[VersionedRecord] | None]],
        recover: Callable[[str], Awaitable[list[VersionedRecord] | None]],
        *,
        expected_result_ids: list[str] | None = None,
        pending_steps: Callable[[], tuple[list[str], list[str]]] | None = None,
    ) -> Receipt:
        rt = self.rt
        known = await self.known(authority, request)
        if known is not None:
            if known.receipt is not None:
                return known.receipt
            results = await recover(known.id)
            if results is None:
                if known.pending_steps:
                    return self.pending_receipt(known, request)
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            return await self.complete(known, request, results)
        now = rt.supervisor.clock.utc()
        row = OwnerOperation(
            id=derived_id("owner-op", request.scope, request.request_id),
            scope=request.scope,
            version=1,
            created_at=now,
            updated_at=now,
            request_id=request.request_id,
            operator_id=authority.principal_id,
            command_type=str(getattr(request, "type", "unknown")),
            fingerprint=command_fingerprint(request),
            target_owner=owner,
            target_id=request.target,
            expected_result_ids=expected_result_ids or [],
            state="planned",
            expires_at=now + rt.store.receipt_retention,
        )
        async with rt.store.transaction(
            PRINCIPALS[Owner.WORKSPACE], request.scope, request
        ) as unit:
            if unit.replay is not None:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            await unit.put(row, access=rt.readers)
            await unit.complete(
                Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="accepted",
                    resource_id=row.id,
                    operation_id=row.id,
                    version=1,
                )
            )
        try:
            results = await perform(row.id)
            if results is None:
                if pending_steps is None:
                    raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
                done, pending = pending_steps()
                if not pending:
                    raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
                updated = row.model_copy(
                    update={
                        "version": row.version + 1,
                        "updated_at": max(rt.supervisor.clock.utc(), row.updated_at),
                        "state": "reconciliation_required",
                        "completed_steps": done,
                        "pending_steps": pending,
                    }
                )
                await self.save(updated, row.version)
                return self.pending_receipt(updated, request)
            return await self.complete(row, request, results)
        except BaseException as error:
            unknown = row.model_copy(
                update={
                    "version": row.version + 1,
                    "updated_at": max(rt.supervisor.clock.utc(), row.updated_at),
                    "state": "reconciliation_required",
                }
            )
            try:
                await self.save(unknown, row.version)
            except Exception:
                pass  # The durable planned record is also an explicit unknown outcome.
            import asyncio

            if isinstance(error, asyncio.CancelledError):
                raise
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN) from None

    @staticmethod
    def pending_receipt(row: OwnerOperation, request: CommandBase) -> Receipt:
        return Receipt(
            request_id=request.request_id,
            scope=request.scope,
            status="waiting",
            operation_id=row.id,
            resource_id=row.id,
            version=row.version,
        )

    async def save(self, row: OwnerOperation, previous: int) -> None:
        await put(
            self.rt.store,
            self.rt.registry,
            Owner.WORKSPACE,
            row.scope,
            "owner-op-" + row.id + "-" + str(row.version),
            row,
            previous,
            self.rt.readers,
        )

    async def complete(
        self, row: OwnerOperation, request: CommandBase, results: list[VersionedRecord]
    ) -> Receipt:
        if not results:
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        receipt = Receipt(
            request_id=request.request_id,
            scope=request.scope,
            status="completed",
            operation_id=row.id,
            resource_id=results[0].id,
            version=results[0].version,
        )
        updated = row.model_copy(
            update={
                "version": row.version + 1,
                "updated_at": max(self.rt.supervisor.clock.utc(), row.updated_at),
                "state": "completed",
                "result_ids": [r.id for r in results],
                "receipt": receipt,
                "pending_steps": [],
            }
        )
        await self.save(updated, row.version)
        return receipt
