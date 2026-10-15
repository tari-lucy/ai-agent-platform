"""Private dialogue continuity from bounded authoritative records, with fresh evidence checks."""

from typing import TYPE_CHECKING

from ai_agent_application.dialogue import Turn
from ai_agent_application.memory import RecallService
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Principal
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery, Utterance
from ai_agent_contracts.memory import RecallRequest, RecallResult, Source
from ai_agent_contracts.states import DeliveryState

if TYPE_CHECKING:
    from .service import Runtime

OPERATOR = Principal("operator", "operator")
HISTORY_CANDIDATES = 200
MAX_HISTORY_SOURCES = 64  # leave room for current input and delivery evidence in bounded contracts


class PrivateHistory:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.sources: dict[str, SourceRef] = {}

    async def evidence(self, scope: Scope, source: Source) -> tuple[SourceRef, ...] | None:
        own = SourceRef(
            source_id=source.id, version=source.version, scope=scope, availability="available"
        )
        if source.source_type != "character-utterance" or source.adapter_id != "private-panel":
            return (own,)
        if source.external_id is None:
            return None
        store = self.runtime.store
        utterance = await store.repository(Utterance, OPERATOR, scope).get(
            source.external_id, purpose="api_transfer"
        )
        if (
            utterance is None
            or utterance.audience_id != "operator-panel"
            or utterance.channel_ids != ["operator-panel"]
            or not utterance.sources
            or utterance.content != source.content
        ):
            return None
        delivery = await store.repository(Delivery, OPERATOR, scope).get(
            derived_id("panel-delivery", scope, utterance.id), purpose="api_transfer"
        )
        if (
            delivery is None
            or delivery.status is not DeliveryState.DELIVERED
            or delivery.utterance_id != utterance.id
            or delivery.revision != utterance.revision
            or delivery.channel_id != "operator-panel"
        ):
            return None
        for ref in utterance.sources:
            current = await store.repository(Source, OPERATOR, scope).get(
                ref.source_id, purpose="api_transfer"
            )
            if (
                ref.scope.partition != scope.partition
                or current is None
                or current.version != ref.version
            ):
                return None
        return (own, *utterance.sources)

    def remember(self, refs: tuple[SourceRef, ...]) -> bool:
        combined = {**self.sources, **{ref.source_id: ref for ref in refs}}
        if len(combined) > MAX_HISTORY_SOURCES:
            return False
        self.sources = combined
        return True

    async def recent(
        self, scope: Scope, incoming: SourceRef | None, limit: int
    ) -> tuple[Turn, ...]:
        if incoming is None:
            return ()
        repo = self.runtime.store.repository(Source, OPERATOR, scope)
        current = await repo.get(incoming.source_id, purpose="api_transfer")
        if current is None:
            return ()
        rows = await repo.recent(
            limit=HISTORY_CANDIDATES, before=current.created_at, purpose="api_transfer"
        )
        selected: list[tuple[Source, Turn, tuple[SourceRef, ...]]] = []
        for source in rows:
            if source.id == incoming.source_id or source.content is None:
                continue
            if source.source_type == "operator-message" and source.adapter_id == "dialogue":
                role, label = "person", "оператор"
            elif (
                source.source_type == "character-utterance" and source.adapter_id == "private-panel"
            ):
                role, label = "character", "персонаж"
            else:
                continue
            refs = await self.evidence(scope, source)
            if refs is not None and self.remember(refs):
                selected.append((source, Turn(role, source.content, label), refs))
            if len(selected) >= limit:
                break
        selected.sort(key=lambda row: (row[0].occurred_at, row[0].created_at, row[0].id))
        return tuple(turn for _, turn, _ in selected)


class PrivateRecall(RecallService):
    history: PrivateHistory

    async def recall(self, request: RecallRequest) -> RecallResult:
        result = await super().recall(request)
        kept = []
        for fragment in result.fragments:
            source = await self.storage.repository(Source, self.principal, request.scope).get(
                fragment.source.source_id, purpose="api_transfer"
            )
            if source is None or source.version != fragment.source.version:
                continue
            refs = await self.history.evidence(request.scope, source)
            if refs is not None and self.history.remember(refs):
                kept.append(fragment)
        return result.model_copy(
            update={
                "fragments": kept,
                "complete": result.complete and len(kept) == len(result.fragments),
            }
        )
