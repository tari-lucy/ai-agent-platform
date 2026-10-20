"""A short grounded biography: why the character changed, what it lived, what it decided to keep.

A derived view of existing records (StateChange, Episode, ReflectionCheckpoint) built without a
model call. Every item is shown only while every source behind it is current, transferable and,
for the character's own replies, confirmed delivered; a deleted ground removes the item. Nothing
is invented for an empty history. Text stays data inside the `<memory>` tag the rules already
declare as data, and the block is bounded.
"""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ai_agent_application.development.schemas import Patch, ReflectionResult
from ai_agent_application.dialogue.context import BuiltContext, ContextPolicy, clip, data
from ai_agent_application.dialogue.model import SectionInfo
from ai_agent_application.llm import ChatMessage
from ai_agent_application.memory.recall import estimate_tokens
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.persona import ReflectionCheckpoint, StateChange
from ai_agent_contracts.states import ChangeState

from .history import OPERATOR, PrivateHistory

if TYPE_CHECKING:
    from .service import Runtime

CHANGE_WINDOW = 50  # recent change records read to find applied changes and their rollbacks
MAX_CHANGES = 3
EPISODE_WINDOW = 30
MAX_EVENTS = 2
BUDGET_TOKENS = 450
ITEM_TOKENS = 110
# Episodes of a plain private reply are represented by summaries; they are not life events.
ROUTINE_CHOICES = frozenset({"Ответил оператору."})


async def deliverable(
    rt: Runtime, scope: Scope, history: PrivateHistory, refs: list[SourceRef]
) -> tuple[SourceRef, ...] | None:
    """Current, transferable evidence for every ref (with delivery proof), else None."""
    if not refs:
        return None
    grounds: list[SourceRef] = []
    repository = rt.store.repository(Source, OPERATOR, scope)
    for ref in {ref.source_id: ref for ref in refs}.values():
        source = await repository.get(ref.source_id, purpose="api_transfer")
        if (
            ref.scope.partition != scope.partition
            or source is None
            or source.version != ref.version
        ):
            return None
        evidence = await history.evidence(scope, source)
        if evidence is None:
            return None
        grounds.extend(evidence)
    return tuple(grounds)


def value(raw: object) -> str:
    return f"{raw:g}" if isinstance(raw, float) else str(raw)


async def candidates(rt: Runtime, scope: Scope) -> list[tuple[str, str, str, list[SourceRef]]]:
    """(label, date, text, grounds): changes newest first, then events, then a kept state."""
    found: list[tuple[str, str, str, list[SourceRef]]] = []
    changes = await rt.store.repository(StateChange, OPERATOR, scope).recent(
        limit=CHANGE_WINDOW, purpose="api_transfer"
    )
    patches: dict[str, Patch] = {}
    for change in changes:
        parsed = rt.registry.validate(
            change.patch.schema_id, change.patch.schema_version, change.patch.data
        )
        if isinstance(parsed, Patch):
            patches[change.id] = parsed
    undone = {
        c.revision_of
        for c in changes
        if c.status is ChangeState.APPLIED
        and c.revision_of
        and c.id in patches
        and patches[c.id].kind == "rollback"
    }
    shown = 0
    for change in changes:
        patch = patches.get(change.id)
        if patch is None or change.status is not ChangeState.APPLIED or change.id in undone:
            continue
        when = change.updated_at.date().isoformat()
        moved = f"{patch.property}: {value(patch.before)} → {value(patch.after)}"
        if patch.kind == "rollback":
            reason = change.operator_reason or "Изменение отменено оператором."
            found.append(("откат-изменения", when, f"{moved}. {reason}", list(change.evidence)))
        else:
            # The reflection's own uncertainty travels with the reason; never a settled fact.
            text = f"{moved} (уверенность {1 - change.uncertainty:.2g}). {change.interpretation}"
            found.append(("изменение-характера", when, text, list(change.evidence)))
        shown += 1
        if shown == MAX_CHANGES:
            break
    episodes = await rt.store.repository(Episode, OPERATOR, scope).recent(
        limit=EPISODE_WINDOW, purpose="api_transfer"
    )
    events = 0
    for episode in episodes:
        if episode.choice in ROUTINE_CHOICES:
            continue
        text = f"{episode.choice} → {episode.actual_outcome}"
        if episode.interpretation:
            text += f" | интерпретация персонажа: {episode.interpretation}"
        when = episode.created_at.date().isoformat()
        found.append(("событие", when, text, list(episode.sources)))
        events += 1
        if events == MAX_EVENTS:
            break
    checkpoints = await rt.store.repository(ReflectionCheckpoint, OPERATOR, scope).recent(
        limit=5, purpose="api_transfer"
    )
    for checkpoint in checkpoints:
        if not checkpoint.completed:
            continue
        result = rt.registry.validate(
            checkpoint.response.schema_id,
            checkpoint.response.schema_version,
            checkpoint.response.data,
        )
        if isinstance(result, ReflectionResult) and result.keep_state_reason and not result.changes:
            when = checkpoint.updated_at.date().isoformat()
            reason = result.keep_state_reason
            found.append(("вывод-рефлексии-оставить", when, reason, list(checkpoint.sources)))
        break  # only the latest reflection speaks for the current state
    return found


async def include_biography(
    rt: Runtime,
    scope: Scope,
    built: BuiltContext,
    history: PrivateHistory,
    policy: ContextPolicy,
) -> BuiltContext:
    budget = rt.private_api_budget
    if budget is None or not budget.memory_processing:
        return built
    remaining = min(BUDGET_TOKENS, max(0, policy.max_tokens - built.manifest.estimated_tokens))
    if remaining < 40:
        return built
    lines: list[str] = []
    used = 0
    stale = over = 0
    truncated = False
    found = await candidates(rt, scope)
    for label, when, text, refs in found:
        grounds = await deliverable(rt, scope, history, refs)
        if grounds is None:
            stale += 1  # a deleted, revoked or newer ground removes the whole item
            continue
        content, cut = clip(data(text), ITEM_TOKENS)
        partial = ' partial="true"' if cut else ""
        line = f'<memory by="{label}" at="{when}"{partial}>{content}</memory>'
        cost = estimate_tokens("\n" + line)
        if cost > remaining - used or not history.remember(grounds):
            over += 1
            continue
        lines.append(line)
        used += cost
        truncated = truncated or cut
    logging.getLogger(__name__).info(
        "Private biography context: shown=%d stale_source=%d over_budget=%d",
        len(lines),
        stale,
        over,
    )
    if not lines:
        return built
    last = built.messages[-1]
    return replace(
        built,
        messages=(
            *built.messages[:-1],
            ChatMessage(last.role, last.content + "\n" + "\n".join(lines)),
        ),
        manifest=replace(
            built.manifest,
            estimated_tokens=built.manifest.estimated_tokens + used,
            sections=(
                *built.manifest.sections,
                SectionInfo("biography", len(lines), len(found) - len(lines), truncated),
            ),
        ),
    )
