"""Pin the separately authorised helper right at catalogue/attempt commit boundaries."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.policy import scope_key, stamp
from ai_agent_application.agent_activity.book import area
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_application.storage import Owner
from ai_agent_application.workspace_files.guard import current
from ai_agent_contracts.agent_workplace import CapabilityBinding, CapabilityManifest
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import VersionedRecord
from ai_agent_contracts.states import ErrorCode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime


async def publication_check(
    rt: Runtime, admission: WorkAdmission, authorize: Callable[[], Awaitable[None]]
) -> Callable[[AsyncConnection], Awaitable[None]]:
    capability = rt.session_helper_capability
    if capability is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    scope = area(admission.scope)

    async def records() -> list[VersionedRecord]:
        group = await rt.store.read_group(
            (CapabilityManifest, CapabilityBinding, Permission), PRINCIPALS[Owner.WORKSPACE], scope
        )
        return [
            row
            for rows in group.values()
            for row in rows
            if isinstance(row, VersionedRecord)
            and getattr(row, "capability_id", None) == capability
        ]

    before = await records()
    await authorize()  # Existing owner policy decides; the pin does not invent another grant rule.
    after = await records()
    if not before or before != after:
        raise ContractError(ErrorCode.STALE_CONTEXT)

    async def check(connection: AsyncConnection) -> None:
        current(admission)
        now = stamp(rt.supervisor.clock.utc())
        for record in before:
            row = (
                await connection.execute(
                    select(db.records.c.version).where(
                        db.records.c.scope_id == scope_key(scope),
                        db.records.c.kind == type(record).__name__,
                        db.records.c.id == record.id,
                        db.records.c.version == record.version,
                        db.records.c.available.is_(True),
                        db.records.c.deleted.is_(False),
                        (db.records.c.expires_at.is_(None)) | (db.records.c.expires_at > now),
                    )
                )
            ).first()
            if row is None:
                raise ContractError(ErrorCode.STALE_CONTEXT)
        fresh = rt.supervisor.clock.utc()
        if any(isinstance(record, Permission) and record.expires_at <= fresh for record in before):
            raise ContractError(ErrorCode.EXPIRED)
        current(admission)

    return check
