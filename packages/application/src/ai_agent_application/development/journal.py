"""Persona-owned reflection replay; validated results survive a process restart."""

from collections.abc import Sequence
from datetime import datetime

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.persona import ReflectionCheckpoint
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.memory.service import derived_id

from .schemas import ReflectionResult
from .service import DevelopmentController


class StoredReflections:
    def __init__(self, controller: DevelopmentController):
        self.controller = controller

    async def get(self, scope: Scope, trigger: str) -> ReflectionCheckpoint | None:
        return await self.controller.storage.repository(
            ReflectionCheckpoint, self.controller.principal, scope
        ).get(derived_id("reflection", scope, trigger))

    async def last_started(self, scope: Scope) -> datetime | None:
        repo = self.controller.storage.repository(
            ReflectionCheckpoint, self.controller.principal, scope
        )
        after = None
        latest = None
        for _ in range(50):
            page = await repo.list(limit=200, after_id=after)
            for record in page:
                latest = max(latest, record.created_at) if latest else record.created_at
            if len(page) < 200:
                return latest
            after = page[-1].id
        raise ContractError(ErrorCode.RESOURCE_BUSY)

    async def prepare(
        self,
        scope: Scope,
        trigger: str,
        episode_ids: Sequence[str],
        input_digest: str,
        profile_version: int,
        state_version: int,
        sources: Sequence[SourceRef],
        result: ReflectionResult,
    ) -> ReflectionCheckpoint:
        identifier = derived_id("reflection", scope, trigger)
        now = self.controller.clock()
        record = self.controller._record(
            ReflectionCheckpoint,
            dict(
                id=identifier,
                scope=scope.model_dump(mode="json"),
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
                version=1,
                trigger_id=trigger,
                episode_ids=sorted(set(episode_ids)),
                input_digest=input_digest,
                profile_version=profile_version,
                state_version=state_version,
                sources=[ref.model_dump(mode="json") for ref in sources],
                response=dict(
                    schema_id="development.reflection-result",
                    schema_version="1.0",
                    data=result.model_dump(mode="json"),
                ),
                completed=False,
            ),
        )
        await self.controller._write(scope, "reflection-" + identifier, [(record, None)])
        return record

    async def complete(self, scope: Scope, record: ReflectionCheckpoint) -> None:
        current = await self.get(scope, record.trigger_id)
        if current is None:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if current.completed:
            return
        revised = self.controller._revised(
            current,
            scope,
            version=current.version + 1,
            updated_at=max(self.controller.clock(), current.updated_at).isoformat(),
            completed=True,
        )
        await self.controller._write(
            scope, f"reflection-done-{current.id}", [(revised, current.version)]
        )
