"""Development controller: validated, bounded, reversible changes to the live character state.

A change is a proposal with lived episodes as evidence, never an order. It is checked against the
accepted profile (property, range, autonomy, step per episode, budget per window, independent
evidence not yet spent on this property), the story rules and the current state version. Identity,
non-autonomous and text properties go to the operator; nothing becomes APPLIED by waiting.
Applying is one transaction (state version + change record) with compare-and-swap; pending changes
built on an older state become CONFLICT. Rollback is a new compensating change shown beforehand
with its dependents; it never rewrites history. The persona owner writes all records because it
owns ``CharacterState``.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from ai_agent_contracts.api import CommandBase, MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import Record, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.persona import CharacterState, Profile, StateChange
from ai_agent_contracts.states import ChangeState, ErrorCode, RunState
from pydantic import TypeAdapter

from ai_agent_application.dialogue.model import ControlSnapshot
from ai_agent_application.extraction.evidence import classify, experience_groups
from ai_agent_application.fencing import admission_guard
from ai_agent_application.gamemaster.rules import StoryClassifier, StoryRules, screen
from ai_agent_application.memory import MemoryService
from ai_agent_application.memory.service import derived_id
from ai_agent_application.persona.schemas import (
    DevelopmentBound,
    DevelopmentBounds,
    PropertyDefinition,
    PropertyDefinitions,
    ValueMap,
)
from ai_agent_application.storage import (
    Owner,
    Principal,
    ReadPurpose,
    RecordAccess,
    RecordRepository,
    Storage,
)

from .rules import assess, value_error
from .schemas import Patch

C = ChangeState
LIST_PAGE = 200
MAX_RETRIES = 3
PENDING = (C.PROPOSED, C.VALIDATING, C.READY, C.AWAITING_APPROVAL)
EXCLUDED_LAYERS = frozenset({"relationships"})  # owned by social memory
MAX_LAST_CHANGES = 50


class EvidenceReader(Protocol):
    async def episode(
        self, scope: Scope, episode_id: str, *, purpose: ReadPurpose = "private"
    ) -> Episode | None: ...

    async def source(self, scope: Scope, source_id: str) -> Source | None: ...


class MemoryEvidence:
    """``EvidenceReader`` over the memory owner's service (composition passes MemoryService)."""

    def __init__(self, memory: MemoryService) -> None:
        self.memory = memory

    async def episode(
        self, scope: Scope, episode_id: str, *, purpose: ReadPurpose = "private"
    ) -> Episode | None:
        repository = self.memory.storage.repository(Episode, self.memory.principal, scope)
        return await repository.get(episode_id, purpose=purpose)

    async def source(self, scope: Scope, source_id: str) -> Source | None:
        return await self.memory.get_source(scope, source_id)


@dataclass(frozen=True)
class Policy:
    max_uncertainty: float = 0.6


@dataclass(frozen=True)
class Candidate:
    proposal_id: str
    property: str
    new_value: float | str
    interpretation: str
    alternative: str
    uncertainty: float
    expected_manifestation: str
    episode_ids: tuple[str, ...]
    base_version: int
    revision_of: str | None = None


@dataclass(frozen=True)
class Outcome:
    change: StateChange
    reasons: tuple[str, ...]
    duplicate: bool = False


@dataclass(frozen=True)
class RollbackPreview:
    change_id: str
    property: str
    current_value: float | str
    restore_value: float | str
    chain: tuple[str, ...]  # applied changes that will be compensated, newest first
    invalidated_pending: tuple[str, ...]
    state_version: int
    digest: str


@dataclass
class DevelopmentController:
    storage: Storage
    principal: Principal
    extensions: ExtensionRegistry
    evidence: EvidenceReader
    control: Callable[[], ControlSnapshot]
    rules: StoryRules | None = None
    classifier: StoryClassifier | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    policy: Policy = field(default_factory=Policy)
    reader_access: tuple[RecordAccess, ...] = ()

    def __post_init__(self) -> None:
        if self.principal.role != "core_owner" or self.principal.owner is not Owner.PERSONA:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def _write_fence(self) -> AbstractContextManager[None]:
        return self.storage.fence(
            admission_guard(self._autonomous_open), owner=self.principal.owner
        )

    # ---- plumbing ---------------------------------------------------------------------

    def _command(self, scope: Scope, name: str) -> CommandBase:
        return MessageCommand(
            request_id=name[:120],
            scope=scope,
            target="development",
            type="dialogue.message",
            arguments=MessageArguments(text="development"),
        )

    def _repo[R: Record](self, model: type[R], scope: Scope) -> RecordRepository[R]:
        return self.storage.repository(model, self.principal, scope)

    def _record[R: Any](self, model: type[R], payload: dict[str, Any]) -> R:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return from_wire(TypeAdapter(model), encoded, extensions=self.extensions)

    def _revised[R: Any](self, record: R, scope: Scope, **changes: Any) -> R:
        data = json.loads(to_wire(record, extensions=self.extensions))
        data.update(changes, scope=scope.model_dump(mode="json"))
        return self._record(type(record), data)

    async def _write(
        self, scope: Scope, name: str, puts: Sequence[tuple[Record, int | None]]
    ) -> None:
        material = [
            json.loads(to_wire(record, extensions=self.extensions)) | {"expected": expected}
            for record, expected in puts
        ]
        command = self._command(scope, name).model_copy(
            update={
                "arguments": MessageArguments(
                    text=hashlib.sha256(
                        json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
                )
            }
        )
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return
            for record, expected in puts:
                await unit.put(record, expected_version=expected, access=self.reader_access or None)
            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=puts[0][0].id,
                    version=next(
                        getattr(record, "version", 1)
                        for record, _ in reversed(puts)
                        if record.id == puts[0][0].id
                    ),
                )
            )

    async def _all[R: Record](self, model: type[R], scope: Scope) -> list[R]:
        repository = self._repo(model, scope)
        found: list[R] = []
        after: str | None = None
        while True:
            page = await repository.list(limit=LIST_PAGE, after_id=after)
            found.extend(page)
            if len(page) < LIST_PAGE:
                return found
            after = page[-1].id

    def _autonomous_open(self) -> bool:
        snap = self.control()
        return (
            snap.admitted
            and snap.run_state is RunState.ACTIVE
            and snap.activity.value == "OPEN"
            and snap.autonomy == "autonomous"
        )

    async def _context(
        self, scope: Scope, *, purpose: ReadPurpose = "private"
    ) -> tuple[Profile, CharacterState, dict[str, PropertyDefinition], dict[str, DevelopmentBound]]:
        profile = await self._repo(Profile, scope).get(scope.persona_id, purpose=purpose)
        state = await self._repo(CharacterState, scope).get(
            "state:" + scope.persona_id, purpose=purpose
        )
        if profile is None or state is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        definitions = self.extensions.validate(
            profile.property_definitions.schema_id,
            profile.property_definitions.schema_version,
            profile.property_definitions.data,
        )
        bounds = self.extensions.validate(
            profile.development_bounds.schema_id,
            profile.development_bounds.schema_version,
            profile.development_bounds.data,
        )
        assert isinstance(definitions, PropertyDefinitions) and isinstance(
            bounds, DevelopmentBounds
        )
        return (
            profile,
            state,
            {d.name: d for d in definitions.properties if d.layer not in EXCLUDED_LAYERS},
            {b.name: b for b in bounds.bounds},
        )

    def _values(self, state: CharacterState) -> dict[str, float | str]:
        parsed = self.extensions.validate(
            state.values.schema_id, state.values.schema_version, state.values.data
        )
        assert isinstance(parsed, ValueMap)
        return dict(parsed.values)

    def _patch(self, change: StateChange) -> Patch:
        parsed = self.extensions.validate(
            change.patch.schema_id, change.patch.schema_version, change.patch.data
        )
        assert isinstance(parsed, Patch)
        return parsed

    # ---- evidence and budget ----------------------------------------------------------

    async def _new_episodes(
        self, scope: Scope, candidate: Candidate, history: Sequence[StateChange]
    ) -> tuple[int, list[SourceRef], str | None]:
        spent_sources = {
            ref.source_id
            for change in history
            if change.status is C.APPLIED
            and self._patch(change).property == candidate.property
            and self._patch(change).kind == "change"
            for ref in change.evidence
        }
        evidence: dict[str, SourceRef] = {}
        groups = experience_groups(await self._all(Episode, scope))
        spent_groups = {groups.get(source_id, source_id) for source_id in spent_sources}
        counted: set[str] = set()
        for episode_id in dict.fromkeys(candidate.episode_ids):
            episode = await self.evidence.episode(scope, episode_id)
            if episode is None or episode.scope.partition != scope.partition:
                return 0, [], "UNKNOWN_EPISODE"
            expected = "simulation" if scope.memory_mode == "test" else "observation"
            if episode.provenance != expected:
                return 0, [], "EPISODE_PROVENANCE"
            independent = False
            for ref in episode.sources:
                source = await self.evidence.source(scope, ref.source_id)
                if (
                    source is None
                    or source.version != ref.version
                    or source.availability != "available"
                ):
                    return 0, [], "SOURCE_UNAVAILABLE"
                verdict = classify(source, scope)
                independent = independent or bool(
                    verdict and verdict.independent and verdict.reject is None
                )
                evidence[ref.source_id] = ref
            fresh = all(r.source_id not in spent_sources for r in episode.sources)
            if independent and fresh:
                group = groups.get(episode.sources[0].source_id, episode.id)
                if group not in spent_groups:
                    counted.add(group)
        return len(counted), list(evidence.values()), None

    def _window_spent(
        self, history: Sequence[StateChange], name: str, window_days: int, now: datetime
    ) -> float:
        undone = {
            c.revision_of for c in history if c.revision_of and self._patch(c).kind == "rollback"
        }
        total = 0.0
        for change in history:
            patch = self._patch(change)
            if (
                change.status is C.APPLIED
                and patch.kind == "change"
                and patch.property == name
                and change.id not in undone
                and now - change.updated_at <= timedelta(days=window_days)
                and not isinstance(patch.before, str)
                and not isinstance(patch.after, str)
            ):
                total += abs(patch.after - patch.before)
        return total

    # ---- submit -----------------------------------------------------------------------

    async def submit(self, scope: Scope, candidate: Candidate) -> Outcome:
        change_id = derived_id("chg", scope, candidate.proposal_id)
        existing = await self._repo(StateChange, scope).get(change_id)
        if existing is not None:
            digest = (
                "input-"
                + hashlib.sha256(
                    json.dumps(asdict(candidate), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
            )
            if digest not in existing.checks:
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            return Outcome(existing, (), duplicate=True)
        profile, state, definitions, bounds = await self._context(scope)
        history = await self._all(StateChange, scope)
        now = self.clock()
        errors: list[str] = []
        reasons: list[str] = []
        checks: list[str] = []
        definition = definitions.get(candidate.property)
        values = self._values(state)
        status = C.READY
        evidence: list[SourceRef] = []
        if definition is None or candidate.property not in values:
            errors.append("UNKNOWN_PROPERTY")
        else:
            before = values[candidate.property]
            if candidate.base_version != state.version:
                status, errors = C.CONFLICT, ["STALE_BASE"]
            if candidate.uncertainty > self.policy.max_uncertainty:
                errors.append("TOO_UNCERTAIN")
            count, evidence, bad = await self._new_episodes(scope, candidate, history)
            if bad is not None:
                errors.append(bad)
            elif count == 0 and not evidence:
                errors.append("NO_EVIDENCE")
            bound = bounds.get(candidate.property)
            if not errors:
                spent = self._window_spent(
                    history, candidate.property, bound.window_days if bound else 7, now
                )
                result = assess(definition, bound, before, candidate.new_value, count, spent)
                errors.extend(result.errors)
                reasons.extend(result.reasons)
                checks.append(f"EVIDENCE_EPISODES_{count}")
            verdict = await self._story(candidate, definition)
            if verdict is not None:
                if verdict.startswith("HARD:"):
                    errors.append(verdict)
                else:
                    reasons.append(verdict)
        if errors and status is not C.CONFLICT:
            status = C.REJECTED
        elif not errors:
            status = C.AWAITING_APPROVAL if reasons else C.READY
        if not evidence:
            # StateChange needs at least one source; fall back to the cited episodes' own sources
            for episode_id in candidate.episode_ids:
                episode = await self.evidence.episode(scope, episode_id)
                if episode is not None:
                    evidence.extend(episode.sources)
        if not evidence:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        before_value = values.get(candidate.property, candidate.new_value)
        base = self._change_payload(
            scope, change_id, candidate, before_value, evidence, now, checks + reasons + errors
        )
        proposed = self._record(StateChange, {**base, "status": C.PROPOSED.value, "version": 1})
        final = self._revised(
            proposed,
            scope,
            status=status.value,
            version=2,
            updated_at=max(self.clock(), proposed.updated_at).isoformat(),
        )
        await self._write(scope, f"chg-{change_id}", [(proposed, None), (final, 1)])
        return Outcome(final, tuple(errors or reasons))

    def _change_payload(
        self,
        scope: Scope,
        change_id: str,
        candidate: Candidate,
        before: float | str,
        evidence: Sequence[SourceRef],
        now: datetime,
        checks: Sequence[str],
    ) -> dict[str, Any]:
        unique = list({ref.source_id: ref for ref in evidence}.values())
        return {
            "id": change_id,
            "scope": scope.model_dump(mode="json"),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "proposal_id": candidate.proposal_id,
            "owner": "persona",
            "base_version": candidate.base_version,
            "patch": {
                "schema_id": "development.patch",
                "schema_version": "1.0",
                "data": Patch(
                    property=candidate.property, before=before, after=candidate.new_value
                ).model_dump(mode="json"),
            },
            "evidence": [ref.model_dump(mode="json") for ref in unique],
            "interpretation": candidate.interpretation,
            "uncertainty": candidate.uncertainty,
            "expected_manifestation": candidate.expected_manifestation,
            "checks": [
                "input-"
                + hashlib.sha256(
                    json.dumps(asdict(candidate), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                *[f"c-{_slug(c)}" for c in dict.fromkeys(checks)][:49],
            ],
            "operator_decision_id": None,
            "new_version": None,
            "revision_of": candidate.revision_of,
        }

    async def _story(self, candidate: Candidate, definition: PropertyDefinition) -> str | None:
        if self.rules is None:
            return None
        result = await screen(
            self.rules,
            f"{definition.title}\n{candidate.interpretation}\n{candidate.expected_manifestation}",
            self.classifier,
        )
        return None if result.allowed else result.reason

    # ---- apply / decide ---------------------------------------------------------------

    async def apply(
        self,
        scope: Scope,
        change_id: str,
        operator_decision_id: str | None = None,
        *,
        operator_reason: str | None = None,
    ) -> StateChange:
        if operator_decision_id is not None:
            return await self._apply(
                scope, change_id, operator_decision_id, operator_reason=operator_reason
            )
        with self._write_fence():
            return await self._apply(scope, change_id)

    async def _apply(
        self,
        scope: Scope,
        change_id: str,
        operator_decision_id: str | None = None,
        *,
        operator_reason: str | None = None,
    ) -> StateChange:
        """Apply a READY change autonomously (control open) or an approved one (operator)."""
        change = await self._repo(StateChange, scope).get(change_id)
        if change is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        operator = operator_decision_id is not None
        if change.status is C.READY and not operator:
            if not self._autonomous_open():
                raise ContractError(ErrorCode.STALE_CONTEXT)  # paused / stopped: stays READY
        elif change.status in (C.READY, C.AWAITING_APPROVAL) and operator:
            pass
        else:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        profile, state, definitions, bounds = await self._context(scope)
        patch = self._patch(change)
        values = self._values(state)
        definition = definitions.get(patch.property)
        if (
            state.version != change.base_version
            or profile.version != state.profile_version
            or definition is None
            or values.get(patch.property) != patch.before
        ):
            await self._set_status(scope, change, C.CONFLICT)
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, state.version)
        if value_error(definition, patch.after) is not None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        if not operator:  # the autonomous budget may have been spent since it was validated
            history = await self._all(StateChange, scope)
            bound = bounds.get(patch.property)
            spent = self._window_spent(
                [h for h in history if h.id != change.id],
                patch.property,
                bound.window_days if bound else 7,
                self.clock(),
            )
            fresh = await self._new_episodes_for(scope, change, history)
            result = assess(definition, bound, patch.before, patch.after, fresh, spent)
            if result.errors or result.route != "autonomous":
                await self._set_status(scope, change, C.CONFLICT)
                raise ContractError(ErrorCode.STALE_CONTEXT)
        now = max(self.clock(), state.updated_at, change.updated_at)
        values[patch.property] = patch.after
        new_state = self._revised(
            state,
            scope,
            version=state.version + 1,
            state_version=state.version + 1,
            updated_at=now.isoformat(),
            values={
                "schema_id": "persona.state-values",
                "schema_version": "1.0",
                "data": ValueMap(values=values).model_dump(mode="json"),
            },
            last_change_ids=[*state.last_change_ids, change.id][-MAX_LAST_CHANGES:],
        )
        applied = self._revised(
            change,
            scope,
            status=C.APPLIED.value,
            version=change.version + 1,
            updated_at=now.isoformat(),
            new_version=state.version + 1,
            operator_decision_id=operator_decision_id,
            operator_reason=operator_reason,
        )
        puts: list[tuple[Record, int | None]] = [
            (new_state, state.version),
            (applied, change.version),
        ]
        puts.extend(await self._stale_puts(scope, new_state.version, exclude=change.id))
        await self._write(scope, f"chga-{change.id}-{applied.version}", puts)
        return applied

    async def _new_episodes_for(
        self, scope: Scope, change: StateChange, history: Sequence[StateChange]
    ) -> int:
        """Independent, unspent experiences behind an existing change (recomputed at apply)."""
        name = self._patch(change).property
        spent = {
            ref.source_id
            for h in history
            if h.id != change.id
            and h.status is C.APPLIED
            and self._patch(h).property == name
            and self._patch(h).kind == "change"
            for ref in h.evidence
        }
        mapping = experience_groups(await self._all(Episode, scope))
        spent_groups = {mapping.get(source_id, source_id) for source_id in spent}
        groups: set[str] = set()
        for ref in change.evidence:
            source = await self.evidence.source(scope, ref.source_id)
            if (
                source is None
                or source.version != ref.version
                or source.scope.partition != scope.partition
                or source.availability != "available"
                or ref.source_id in spent
            ):
                continue
            verdict = classify(source, scope)
            if verdict.independent and verdict.reject is None:
                group = mapping.get(ref.source_id, ref.source_id)
                if group not in spent_groups:
                    groups.add(group)
        return len(groups)

    async def _group(self, scope: Scope, source_id: str) -> str:
        groups = experience_groups(await self._all(Episode, scope))
        return groups.get(source_id, source_id)

    async def _set_status(
        self, scope: Scope, change: StateChange, status: ChangeState
    ) -> StateChange:
        revised = self._revised(
            change,
            scope,
            status=status.value,
            version=change.version + 1,
            updated_at=max(self.clock(), change.updated_at).isoformat(),
        )
        await self._write(scope, f"chgs-{change.id}-{revised.version}", [(revised, change.version)])
        return revised

    async def decide(
        self,
        scope: Scope,
        change_id: str,
        approve: bool,
        operator_decision_id: str,
        *,
        reason: str | None = None,
    ) -> StateChange:
        """Operator verdict on a waiting change. Approval applies it; silence never does."""
        change = await self._repo(StateChange, scope).get(change_id)
        if change is None or change.status not in (C.AWAITING_APPROVAL, C.READY):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if approve:
            return await self.apply(scope, change_id, operator_decision_id, operator_reason=reason)
        revised = self._revised(
            change,
            scope,
            status=C.REJECTED.value,
            version=change.version + 1,
            operator_decision_id=operator_decision_id,
            operator_reason=reason,
            updated_at=max(self.clock(), change.updated_at).isoformat(),
        )
        await self._write(scope, f"chgr-{change.id}-{revised.version}", [(revised, change.version)])
        return revised

    async def correct_interpretation(
        self, scope: Scope, change_id: str, text: str, reason: str, decision_id: str
    ) -> StateChange:
        """Explicit operator annotation; immutable revisions retain the previous reading.

        The event/evidence and accumulated CharacterState do not change. A pending
        proposal needs another explicit decision after its interpretation changes.
        """
        change = await self._repo(StateChange, scope).get(change_id)
        if change is None or change.status not in (C.READY, C.AWAITING_APPROVAL, C.APPLIED):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if not text.strip() or not reason.strip() or len(text) > 1500 or len(reason) > 1000:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        revised = self._revised(
            change,
            scope,
            version=change.version + 1,
            updated_at=max(self.clock(), change.updated_at).isoformat(),
            interpretation=text,
            operator_reason=reason,
            operator_decision_id=decision_id,
            status=C.APPLIED.value if change.status is C.APPLIED else C.AWAITING_APPROVAL.value,
        )
        await self._write(scope, f"chgi-{change.id}-{revised.version}", [(revised, change.version)])
        return revised

    async def cancel(self, scope: Scope, change_id: str) -> StateChange:
        change = await self._repo(StateChange, scope).get(change_id)
        if change is None or change.status not in PENDING:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return await self._set_status(scope, change, C.CANCELLED)

    async def _stale_puts(
        self, scope: Scope, state_version: int, *, exclude: str | None = None
    ) -> list[tuple[Record, int | None]]:
        """Keep invalidation in the same owner transaction as the state revision."""
        return [
            (
                self._revised(
                    change,
                    scope,
                    status=C.CONFLICT.value,
                    version=change.version + 1,
                    updated_at=max(self.clock(), change.updated_at).isoformat(),
                ),
                change.version,
            )
            for change in await self._all(StateChange, scope)
            if change.id != exclude
            and change.status in (C.READY, C.AWAITING_APPROVAL)
            and change.base_version < state_version
        ]

    async def invalidate_stale(self, scope: Scope) -> list[str]:
        """Pending changes built on an older state version can no longer be applied blindly."""
        state = await self._repo(CharacterState, scope).get("state:" + scope.persona_id)
        if state is None:
            return []
        stale = []
        for change in await self._all(StateChange, scope):
            if (
                change.status in (C.READY, C.AWAITING_APPROVAL)
                and change.base_version < state.version
            ):
                await self._set_status(scope, change, C.CONFLICT)
                stale.append(change.id)
        return stale

    async def history(self, scope: Scope, property: str | None = None) -> list[StateChange]:
        found = [
            c
            for c in await self._all(StateChange, scope)
            if property is None or self._patch(c).property == property
        ]
        return sorted(found, key=lambda c: (c.created_at, c.id))

    # ---- rollback ---------------------------------------------------------------------

    async def preview_rollback(self, scope: Scope, change_id: str) -> RollbackPreview:
        history = await self._all(StateChange, scope)
        target = next((c for c in history if c.id == change_id), None)
        if target is None or target.status is not C.APPLIED or self._patch(target).kind != "change":
            raise ContractError(ErrorCode.STALE_CONTEXT)
        undone = {
            c.revision_of for c in history if c.revision_of and self._patch(c).kind == "rollback"
        }
        if target.id in undone:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        name = self._patch(target).property
        chain = sorted(
            (
                c
                for c in history
                if c.status is C.APPLIED
                and self._patch(c).kind == "change"
                and self._patch(c).property == name
                and c.id not in undone
                and (c.new_version or 0) >= (target.new_version or 0)
            ),
            key=lambda c: c.new_version or 0,
            reverse=True,
        )
        _, state, _, _ = await self._context(scope)
        current = self._values(state)[name]
        if current != self._patch(chain[0]).after:
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, state.version)
        pending = tuple(c.id for c in history if c.status in (C.READY, C.AWAITING_APPROVAL))
        restore = self._patch(target).before
        digest = hashlib.sha256(
            json.dumps(
                [change_id, [c.id for c in chain], current, restore, state.version, list(pending)],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return RollbackPreview(
            change_id,
            name,
            current,
            restore,
            tuple(c.id for c in chain),
            pending,
            state.version,
            digest,
        )

    async def rollback(
        self, scope: Scope, change_id: str, operator_decision_id: str, preview_digest: str
    ) -> list[StateChange]:
        """Compensate the change and everything built on it; the operator saw the preview."""
        preview = await self.preview_rollback(scope, change_id)
        if preview.digest != preview_digest:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        _, state, definitions, _ = await self._context(scope)
        if state.version != preview.state_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, None, state.version)
        definition = definitions.get(preview.property)
        if definition is None or value_error(definition, preview.restore_value) is not None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        history = {c.id: c for c in await self._all(StateChange, scope)}
        now = max(self.clock(), state.updated_at)
        values = self._values(state)
        values[preview.property] = preview.restore_value
        new_state = self._revised(
            state,
            scope,
            version=state.version + 1,
            state_version=state.version + 1,
            updated_at=now.isoformat(),
            values={
                "schema_id": "persona.state-values",
                "schema_version": "1.0",
                "data": ValueMap(values=values).model_dump(mode="json"),
            },
        )
        puts: list[tuple[Record, int | None]] = [(new_state, state.version)]
        compensations: list[StateChange] = []
        for original_id in preview.chain:
            original = history[original_id]
            patch = self._patch(original)
            record = self._record(
                StateChange,
                {
                    "id": derived_id("chg", scope, "rollback", original_id),
                    "scope": scope.model_dump(mode="json"),
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "version": 1,
                    "proposal_id": f"rollback:{original_id}",
                    "owner": "persona",
                    "base_version": state.version,
                    "patch": {
                        "schema_id": "development.patch",
                        "schema_version": "1.0",
                        "data": Patch(
                            property=patch.property,
                            before=patch.after,
                            after=patch.before,
                            kind="rollback",
                        ).model_dump(mode="json"),
                    },
                    "evidence": [r.model_dump(mode="json") for r in original.evidence],
                    "interpretation": f"Откат изменения {original_id} по решению оператора.",
                    "uncertainty": 0,
                    "expected_manifestation": "Прежнее значение свойства восстановлено.",
                    "checks": [],
                    "status": C.APPLIED.value,
                    "operator_decision_id": operator_decision_id,
                    "new_version": state.version + 1,
                    "revision_of": original_id,
                },
            )
            puts.append((record, None))
            compensations.append(record)
        puts.extend(await self._stale_puts(scope, new_state.version))
        await self._write(scope, f"chgb-{change_id}-{state.version}", puts)
        return compensations


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "-" for c in text)[:60]


__all__ = [
    "Candidate",
    "DevelopmentController",
    "MemoryEvidence",
    "Outcome",
    "Policy",
    "RollbackPreview",
    "Source",
]
