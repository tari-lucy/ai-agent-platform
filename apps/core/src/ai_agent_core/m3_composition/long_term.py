"""Bounded derived private memory, with current source and exact delivery checks."""

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from ai_agent_application.dialogue.context import BuiltContext, ContextPolicy, clip, data
from ai_agent_application.dialogue.model import SectionInfo
from ai_agent_application.llm import ChatMessage
from ai_agent_application.memory.recall import estimate_tokens
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import Claim, Source, Summary

from .history import OPERATOR, PrivateHistory

if TYPE_CHECKING:
    from .service import Runtime


async def include_memory(
    rt: Runtime,
    scope: Scope,
    built: BuiltContext,
    history: PrivateHistory,
    query: str,
    policy: ContextPolicy,
) -> BuiltContext:
    budget = rt.private_api_budget
    if budget is None or not budget.memory_processing:
        return built
    remaining = min(650, max(0, policy.max_tokens - built.manifest.estimated_tokens))
    if remaining < 30:
        return built
    summaries = await rt.store.repository(Summary, OPERATOR, scope).recent(
        limit=1, purpose="api_transfer"
    )
    claims = await rt.store.repository(Claim, OPERATOR, scope).recent(
        limit=10, purpose="api_transfer"
    )
    words = set(re.findall(r"[^\W_]{4,}", query.lower()))
    candidates: list[Summary | Claim] = [*summaries, *claims]
    fragments, used, truncated = [], 0, False
    for record in candidates:
        if (
            isinstance(record, Claim)
            and words
            and not any(word in record.content.lower() for word in words)
        ):
            continue
        grounds: list[SourceRef] = []
        valid = True
        for ref in record.sources:
            source = await rt.store.repository(Source, OPERATOR, scope).get(
                ref.source_id, purpose="api_transfer"
            )
            if (
                ref.scope.partition != scope.partition
                or source is None
                or source.version != ref.version
            ):
                valid = False
                break
            evidence = await history.evidence(scope, source)
            if evidence is None:
                valid = False
                break
            grounds.extend(evidence)
        if not valid:
            continue
        label = (
            "сводка-интерпретация"
            if isinstance(record, Summary)
            else "утверждение-" + record.knowledge_type
        )
        capacity = min(350 if isinstance(record, Summary) else 200, remaining - used)
        if capacity < 45:
            continue
        content, cut = clip(data(record.content), capacity - 40)
        partial = ' partial="true"' if cut else ""
        fragment = f'<memory by="{label}"{partial}>{content}</memory>'
        cost = estimate_tokens("\n" + fragment)
        if cost > remaining - used or not history.remember(tuple(grounds)):
            continue
        fragments.append(fragment)
        truncated = truncated or cut
        used += cost
        if len(fragments) == 3:
            break
    if not fragments:
        return built
    last = built.messages[-1]
    return replace(
        built,
        messages=(
            *built.messages[:-1],
            ChatMessage(last.role, last.content + "\n" + "\n".join(fragments)),
        ),
        manifest=replace(
            built.manifest,
            estimated_tokens=built.manifest.estimated_tokens + used,
            sections=(
                *built.manifest.sections,
                SectionInfo(
                    "long_term", len(fragments), len(candidates) - len(fragments), truncated
                ),
            ),
        ),
    )
