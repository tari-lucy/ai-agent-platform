"""Read-only integrity gate before owner reconciliation; orphan records cannot hide in a join."""

from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.control import Job
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter
from sqlalchemy import text

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key

MAX_JOBS = 10000


async def check_inventory(store: SQLiteStore) -> None:
    """Validate all current Jobs and their schedule keys, including otherwise invisible orphans.

    No business writes: only JobOwner decides CANCELLED/OUTCOME_UNKNOWN. A bounded overflow or
    malformed record closes startup instead of silently declaring its old operation recovered.
    """
    async with store.connection() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT r.scope_id,r.id,r.version,v.payload,s.operation_id FROM records r "
                    "LEFT JOIN record_revisions v ON r.scope_id=v.scope_id AND r.kind=v.kind "
                    "AND r.id=v.id AND r.version=v.version LEFT JOIN job_schedules s "
                    "ON s.scope_id=r.scope_id AND s.job_id=r.id WHERE r.kind='Job' "
                    "ORDER BY r.scope_id,r.id LIMIT :limit"
                ),
                {"limit": MAX_JOBS + 1},
            )
        ).all()
    # Retention removes terminal Job records; leftover inert schedules alone are harmless.
    if len(rows) > MAX_JOBS:
        raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
    for partition, identifier, version, payload, operation in rows:
        if not isinstance(payload, str) or operation is None:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        job = from_wire(TypeAdapter(Job), payload.encode(), extensions=store.extensions)
        if (
            scope_key(job.scope) != partition
            or job.id != identifier
            or job.version != version
            or job.operation_id != operation
        ):
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
