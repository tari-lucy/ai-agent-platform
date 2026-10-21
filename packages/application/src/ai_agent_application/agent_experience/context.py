"""The person in a workplace step (AW-06): a minimal, rights-checked brief over existing owners.

Canon, style and current state come from the persona owner; open obligations from the social
owner; relevant memory from recall. Everything sent to the model needs api_transfer: an
obligation or fragment whose sources are not transferable is left out, never summarised. The
sources shown become inputs of the attempt, so whatever the step produces keeps that lineage.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import RecallRequest

from ai_agent_application.dialogue.context import data
from ai_agent_application.dialogue.ports import MemoryPort, ProfileSource, RecallPort, SocialPort


@dataclass(frozen=True)
class BriefLimits:
    canon_chars: int = 2500
    style_chars: int = 600
    state_values: int = 12
    obligations: int = 8
    recall: int = 5
    recall_tokens: int = 600


@dataclass(frozen=True)
class PersonaBrief:
    text: str
    sources: tuple[SourceRef, ...]
    omitted: int  # obligations/fragments left out for rights or bounds


class PersonaContext:
    def __init__(
        self,
        profile: ProfileSource,
        memory: MemoryPort,
        social: SocialPort | None = None,
        recall: RecallPort | None = None,
        limits: BriefLimits | None = None,
    ) -> None:
        self.profile, self.memory, self.social, self.recall = profile, memory, social, recall
        self.limits = limits or BriefLimits()

    async def _transferable(self, scope: Scope, sources: Sequence[SourceRef]) -> bool:
        if not sources:
            return False
        for ref in sources:
            found = await self.memory.get_source(scope, ref.source_id)
            if found is None or found.availability != "available" or not found.api_transfer_allowed:
                return False
        return True

    async def build(self, scope: Scope, query: str) -> PersonaBrief:
        limits = self.limits
        profile = await self.profile.get_profile(scope)
        if profile is None:
            return PersonaBrief("", (), 0)
        lines = [
            "Характер (канон): " + data(profile.canon[: limits.canon_chars]),
            "Стиль: " + data(profile.style[: limits.style_chars]),
        ]
        state = await self.profile.get_state(scope)
        values: dict[str, Any] = {}
        if state is not None and isinstance(state.values.data, dict):
            raw = state.values.data.get("values")
            if isinstance(raw, dict):
                values = dict(list(raw.items())[: limits.state_values])
        if values:
            lines.append(
                "Текущее состояние: "
                + "; ".join(f"{data(str(k))}={data(str(v))}" for k, v in values.items())
            )
        sources: list[SourceRef] = []
        omitted = 0
        if self.social is not None:
            shown: list[str] = []
            for item in await self.social.obligations(scope):
                promise = item.promise
                if len(shown) >= limits.obligations or not await self._transferable(
                    scope, promise.sources
                ):
                    omitted += 1
                    continue
                due = "" if promise.deadline is None else f" (срок {promise.deadline.date()})"
                shown.append("- " + data(promise.content[:300]) + due)
                sources.extend(promise.sources)
            if shown:
                lines.append("Открытые обязательства:\n" + "\n".join(shown))
        if self.recall is not None and query.strip():
            result = await self.recall.recall(
                RecallRequest(
                    scope=scope,
                    query=query[:1000],
                    limit=limits.recall,
                    context_token_limit=limits.recall_tokens,
                    purpose="api_transfer",
                )
            )
            remembered = []
            for fragment in result.fragments[: limits.recall]:
                if not await self._transferable(scope, (fragment.source,)):
                    omitted += 1
                    continue
                remembered.append("- " + data(fragment.content[:400]))
                sources.append(fragment.source)
            if remembered:
                lines.append("Что я помню по теме:\n" + "\n".join(remembered))
        unique = {(ref.source_id, ref.version): ref for ref in sources}
        return PersonaBrief("\n".join(lines), tuple(unique.values()), omitted)
