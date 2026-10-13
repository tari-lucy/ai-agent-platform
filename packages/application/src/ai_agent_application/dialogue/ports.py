"""Narrow ports of the dialogue controller. Real services satisfy them structurally."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Protocol

from ai_agent_contracts.api import CommandBase
from ai_agent_contracts.common import Id, Scope, SourceRef
from ai_agent_contracts.control import ActionProposal
from ai_agent_contracts.memory import Promise, RecallRequest, RecallResult, Source
from ai_agent_contracts.persona import CharacterState, Profile

from ai_agent_application.llm import Generation, GenerationRequest

from .model import ControlSnapshot

type DeltaCallback = Callable[[str], Awaitable[None]]


class Generator(Protocol):
    async def generate(
        self,
        request: GenerationRequest,
        cancel: Any = None,
        on_delta: DeltaCallback | None = None,
    ) -> Generation: ...


class ProfileSource(Protocol):
    async def get_profile(self, scope: Scope, version: int | None = None) -> Profile | None: ...

    async def get_state(self, scope: Scope) -> CharacterState | None: ...


class MemoryPort(Protocol):
    async def register_source(self, command: CommandBase, draft: Any) -> Any: ...

    async def record_episode(self, command: CommandBase, draft: Any) -> Any: ...

    async def get_source(self, scope: Scope, source_id: str) -> Source | None: ...


class RecallPort(Protocol):
    async def recall(self, request: RecallRequest) -> RecallResult: ...


class SocialPort(Protocol):
    async def observe_account(
        self,
        command: CommandBase,
        platform_id: Id,
        account_id: str,
        display_name: str,
        evidence: SourceRef,
    ) -> Any: ...

    async def context_for(self, scope: Scope, person_id: str) -> Any: ...

    async def obligations(self, scope: Scope, person_id: str | None = None) -> Sequence[Any]: ...

    async def record_encounter(
        self,
        command: CommandBase,
        person_ids: Sequence[str],
        sources: Sequence[SourceRef],
        key: str,
    ) -> Any: ...

    async def reserve_question(
        self, command: CommandBase, person_id: str, topic_key: str, operation_id: Id
    ) -> Any: ...

    async def settle_question(
        self,
        command: CommandBase,
        person_id: str,
        topic_key: str,
        operation_id: Id,
        outcome: str,
        utterance_id: Id | None = None,
    ) -> Any: ...

    async def record_promise(
        self,
        command: CommandBase,
        participant_ids: Sequence[str],
        topic_key: str,
        *,
        delivery_id: Id,
        utterance_id: Id,
        promise_key: str,
        content: str,
        completion_criterion: str,
        sources: Sequence[SourceRef],
        deadline: datetime | None = None,
    ) -> tuple[Promise, bool]: ...


class ControlView(Protocol):
    """Trusted supervisor view; the dialogue controller only reads it."""

    def snapshot(self) -> ControlSnapshot: ...


class ProposalSink(Protocol):
    """Hand-off of a validated action proposal to the behavior/job path. It never executes here."""

    async def submit(self, proposal: ActionProposal) -> None: ...


class PrivateStream(Protocol):
    """Provisional text for the operator panel. Not a delivery and not a public output."""

    async def emit(self, event_id: str, text: str) -> None: ...

    async def close(self, event_id: str, status: str) -> None: ...
