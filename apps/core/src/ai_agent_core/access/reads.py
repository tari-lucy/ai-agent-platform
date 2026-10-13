"""Use accepted repository ACL/TTL/privacy filters; no second data owner."""

from typing import cast

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Principal
from ai_agent_contracts.api import Page
from ai_agent_contracts.catalogue import READ_MODELS
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Contract, Record, Scope
from ai_agent_contracts.states import ErrorCode


class SQLiteReader:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        resource_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> Contract | None:
        model = READ_MODELS.get(resource)
        if model is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        repository = self.store.repository(
            cast(type[Record], model), Principal(principal_id, "operator"), scope
        )
        if resource_id is not None:
            return await repository.get(resource_id, purpose="api_transfer")
        rows = await repository.list(limit=limit, after_id=cursor, purpose="api_transfer")
        complete = len(rows) < limit
        return Page[model](  # type: ignore[valid-type]
            items=rows,
            limit=limit,
            next_cursor=None if complete else rows[-1].id,
            complete=complete,
        )
