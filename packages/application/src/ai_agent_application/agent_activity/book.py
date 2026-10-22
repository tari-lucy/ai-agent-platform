"""Activity owner (AW-05): semantic tasks and their attempts; it never runs anything itself.

Execution of an attempt is an existing job (JobOwner); a WorkAttempt only links the task to that
operation and records its inputs. Every write is one owner transaction keyed by request_id.
"""

import builtins
import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime

from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.agent_workplace import AttemptState, WorkAttempt, WorkOrigin, WorkTask
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Record, Scope, SourceRef, VersionedRecord
from ai_agent_contracts.control import Freshness
from ai_agent_contracts.states import ErrorCode

from ai_agent_application.memory import derived_id
from ai_agent_application.storage import (
    Owner,
    Principal,
    RecordAccess,
    RecordRepository,
    Storage,
)

from .model import ORIGIN_ORDER

MAX_SCAN = 2000


def area(scope: Scope) -> Scope:
    return Scope(
        workspace_id=scope.workspace_id,
        persona_id=scope.persona_id,
        memory_space_id=scope.memory_space_id,
        memory_mode=scope.memory_mode,
    )


def attempt_id(task_id: str, number: int) -> str:
    return f"{task_id}.a{number:04d}"


def session_attempt_id(scope: Scope, session_id: str, sequence: int, number: int) -> str:
    return derived_id("session-attempt", area(scope), session_id, str(sequence), str(number))


class ActivityBook:
    def __init__(
        self,
        store: Storage,
        clock: Callable[[], datetime],
        readers: tuple[RecordAccess, ...] = (),
        principal: Principal | None = None,
    ) -> None:
        self.store, self.clock, self.readers = store, clock, readers
        self.principal = principal or Principal("core-activity", "core_owner", Owner.ACTIVITY)

    def tasks_repo(self, scope: Scope) -> RecordRepository[WorkTask]:
        return self.store.repository(WorkTask, self.principal, area(scope))

    def attempts_repo(self, scope: Scope) -> RecordRepository[WorkAttempt]:
        # Attempts carry the run scope; the store partition is the same stable area.
        return self.store.repository(WorkAttempt, self.principal, scope)

    async def _scan[T: VersionedRecord](self, repo: RecordRepository[T]) -> builtins.list[T]:
        rows: builtins.list[T] = []
        cursor: str | None = None
        while len(rows) < MAX_SCAN:
            page = await repo.list(limit=200, after_id=cursor)
            rows += page
            if len(page) < 200:
                break
            cursor = page[-1].id
        return rows

    async def tasks(self, scope: Scope) -> builtins.list[WorkTask]:
        return await self._scan(self.tasks_repo(scope))

    async def attempts(self, scope: Scope, task_id: str) -> builtins.list[WorkAttempt]:
        task = await self.tasks_repo(scope).get(task_id)
        if task is None:
            return []
        repo = self.attempts_repo(scope)
        found: builtins.list[WorkAttempt] = []
        number = 1
        while number <= 1000:
            row = await repo.get(attempt_id(task_id, number))
            if row is None:
                break
            found.append(row)
            number += 1
        return found

    async def _commit(
        self, request_id: str, scope: Scope, rows: Iterable[tuple[Record, int | None]]
    ) -> Receipt:
        items = list(rows)
        digest = hashlib.sha256(
            json.dumps(
                [
                    row.model_dump(mode="json", exclude={"created_at", "updated_at"})
                    for row, _ in items
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target=items[0][0].id,
            type="dialogue.message",
            arguments=MessageArguments(text=digest),
        )
        # The transaction scope is the records' own: stable area for tasks, run for attempts.
        async with self.store.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            for row, expected in items:
                await unit.put(row, expected_version=expected, access=self.readers or None)
            first = items[0][0]
            receipt = Receipt(
                request_id=request_id,
                scope=scope,
                status="completed",
                resource_id=first.id,
                version=getattr(first, "version", 1),
            )
            await unit.complete(receipt)
            return receipt

    async def open_task(
        self,
        request_id: str,
        scope: Scope,
        origin: WorkOrigin,
        goal: str,
        reason: str,
        *,
        limit: int,
        parent_task_id: str | None = None,
    ) -> WorkTask:
        """Record a goal. Bounded: a full queue refuses new work instead of growing."""
        identifier = derived_id("work-task", area(scope), request_id)
        existing = await self.tasks_repo(scope).get(identifier)
        if existing is not None:
            return existing
        open_count = sum(t.state not in ("done", "abandoned") for t in await self.tasks(scope))
        if open_count >= limit:
            raise ContractError(ErrorCode.RESOURCE_BUSY, request_id)
        now = self.clock()
        task = WorkTask(
            id=identifier,
            scope=area(scope),
            created_at=now,
            updated_at=now,
            version=1,
            task_id=identifier,
            origin=origin,
            goal=goal,
            reason=reason,
            state="open",
            parent_task_id=parent_task_id,
        )
        await self._commit(request_id, area(scope), [(task, None)])
        stored = await self.tasks_repo(scope).get(identifier)
        assert stored is not None
        return stored

    async def update_task(self, request_id: str, task: WorkTask, **changes: object) -> WorkTask:
        data = task.model_dump()
        data.update(version=task.version + 1, updated_at=self.clock(), **changes)
        revised = WorkTask.model_validate(data)
        await self._commit(request_id, task.scope, [(revised, task.version)])
        stored = await self.tasks_repo(task.scope).get(task.id)
        assert stored is not None
        return stored

    async def prepare(
        self,
        task: WorkTask,
        number: int,
        operation_id: str,
        scope: Scope,
        freshness: Freshness,
        inputs: builtins.list[SourceRef],
        input_files: builtins.list[ArtifactRef] | None = None,
    ) -> WorkAttempt:
        """The attempt before its job runs; the context service records its inputs here."""
        identifier = attempt_id(task.id, number)
        repo = self.attempts_repo(scope)
        existing = await repo.get(identifier)
        if existing is not None:
            return existing
        now = self.clock()
        row = WorkAttempt(
            id=identifier,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            task_id=task.id,
            attempt_no=number,
            operation_id=operation_id,
            capability_id="m3.activity",
            capability_version=1,
            freshness=freshness,
            state="submitted",
            inputs=inputs,
            input_files=input_files or [],
        )
        await self._commit("prepare-" + identifier, scope, [(row, None)])
        stored = await repo.get(identifier)
        assert stored is not None
        return stored

    async def session_attempts(self, scope: Scope, session_id: str) -> builtins.list[WorkAttempt]:
        rows = await self._scan(self.attempts_repo(scope))
        if len(rows) >= MAX_SCAN:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        return sorted(
            (row for row in rows if row.session_id == session_id),
            key=lambda row: (row.session_sequence or 0, row.attempt_no),
        )

    async def prepare_session(
        self,
        session: WorkSession,
        number: int,
        operation_id: str,
        scope: Scope,
        freshness: Freshness,
        inputs: builtins.list[SourceRef],
        input_files: builtins.list[ArtifactRef],
    ) -> WorkAttempt:
        identifier = session_attempt_id(scope, session.id, session.last_sequence, number)
        existing = await self.attempts_repo(scope).get(identifier)
        if existing is not None:
            return existing
        now = self.clock()
        row = WorkAttempt(
            id=identifier,
            scope=scope,
            version=1,
            created_at=now,
            updated_at=now,
            session_id=session.id,
            session_sequence=session.last_sequence,
            attempt_no=number,
            operation_id=operation_id,
            capability_id="m3.activity",
            capability_version=1,
            freshness=freshness,
            state="submitted",
            inputs=inputs,
            input_files=input_files,
        )
        await self._commit("prepare-" + identifier, scope, [(row, None)])
        return row

    async def bind(
        self, attempt: WorkAttempt, capability_id: str, capability_version: int
    ) -> WorkAttempt:
        """Record the chosen capability before any IO: the file owner authorizes by it."""
        if (attempt.capability_id, attempt.capability_version) == (
            capability_id,
            capability_version,
        ):
            return attempt
        data = attempt.model_dump()
        data.update(
            version=attempt.version + 1,
            updated_at=self.clock(),
            capability_id=capability_id,
            capability_version=capability_version,
        )
        revised = WorkAttempt.model_validate(data)
        await self._commit(
            f"bind-{attempt.id}-{attempt.version}", attempt.scope, [(revised, attempt.version)]
        )
        stored = await self.attempts_repo(attempt.scope).get(attempt.id)
        assert stored is not None
        return stored

    async def settle(
        self,
        attempt: WorkAttempt,
        state: AttemptState,
        result_id: str | None = None,
        *,
        result_files: builtins.list[ArtifactRef] | None = None,
    ) -> WorkAttempt:
        if attempt.state == state:
            return attempt
        data = attempt.model_dump()
        data.update(
            version=attempt.version + 1,
            updated_at=self.clock(),
            state=state,
            result_id=result_id if state == "succeeded" else attempt.result_id,
            result_files=attempt.result_files if result_files is None else result_files,
        )
        revised = WorkAttempt.model_validate(data)
        await self._commit(
            f"settle-{attempt.id}-{attempt.version}", attempt.scope, [(revised, attempt.version)]
        )
        stored = await self.attempts_repo(attempt.scope).get(attempt.id)
        assert stored is not None
        return stored


def order(tasks: Iterable[WorkTask]) -> builtins.list[WorkTask]:
    """Assignments first, then follow-ups, commitments and own goals; oldest first in each."""
    return sorted(tasks, key=lambda t: (ORIGIN_ORDER[t.origin], t.created_at, t.id))
