"""Owner-facing ports; trusted core composition establishes identities and grants."""

import builtins
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.common import Record, Scope


class Owner(StrEnum):
    WORKSPACE = "workspace"
    PERSONA = "persona"
    SUPERVISOR = "supervisor"
    MEMORY = "memory"
    SOCIAL = "social"
    BEHAVIOR = "behavior"
    JOBS = "jobs"
    GATEWAY = "gateway"
    MODERATION = "moderation"
    CHANNEL = "channel"
    GM = "gm"
    LLM = "llm"
    CATALOGUE = "catalogue"
    LABORATORY = "laboratory"


type ReadPurpose = Literal["private", "api_transfer", "public_disclosure"]


@dataclass(frozen=True)
class Principal:
    id: str
    role: Literal["core_owner", "operator", "worker"]
    owner: Owner | None = None


@dataclass(frozen=True)
class Grant:
    principal: Principal
    owner: Owner
    read: bool = False
    write: bool = False
    api_transfer: bool = False
    public_disclosure: bool = False
    expires_at: datetime | None = None


@dataclass(frozen=True)
class RecordAccess:
    principal_id: str
    read: bool = True
    api_transfer: bool = False
    public_disclosure: bool = False
    expires_at: datetime | None = None


class UnitOfWork(Protocol):
    replay: Receipt | None

    async def put(
        self,
        record: Record,
        *,
        expected_version: int | None = None,
        access: tuple[RecordAccess, ...] | None = None,
    ) -> None: ...

    async def complete(self, receipt: Receipt) -> None: ...


class RecordRepository[T: Record](Protocol):
    async def get(
        self, record_id: str, *, purpose: ReadPurpose = "private", version: int | None = None
    ) -> T | None: ...

    async def list(
        self, *, limit: int = 50, after_id: str | None = None, purpose: ReadPurpose = "private"
    ) -> list[T]: ...

    async def recent(
        self,
        *,
        limit: int = 50,
        before: datetime | None = None,
        before_id: str | None = None,
        chronology: bool = False,
        utterance_id: str | None = None,
        cites: Sequence[str] | None = None,
        purpose: ReadPurpose = "private",
    ) -> builtins.list[T]:
        """Newest first, strict time/id boundary; chronology uses Source.occurred_at.

        utterance_id is supported for Delivery only. cites (1..200 source ids, not for Source)
        keeps records whose current revision links one of them. All get/list read policies apply.
        Omitting before_id preserves the inclusive historical before cutoff.
        """
        ...

    async def get_for_lifecycle(self, record_id: str) -> T | None: ...

    async def list_for_lifecycle(
        self, *, limit: int = 50, after_id: str | None = None
    ) -> builtins.list[T]: ...


class Storage(Protocol):
    def fence(
        self, guard: Callable[[], None], *, owner: Owner | None = None
    ) -> AbstractContextManager[None]: ...

    def check_fence(self) -> None: ...

    def repository[T: Record](
        self, model: type[T], principal: Principal, scope: Scope
    ) -> RecordRepository[T]: ...

    def transaction(
        self, principal: Principal, scope: Scope, command: CommandBase
    ) -> AbstractAsyncContextManager[UnitOfWork]: ...
