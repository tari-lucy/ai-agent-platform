"""Dialogue / context / attention task controller (M2-06).

One controller serves one session scope. A signal is admitted by attention, understood in a bounded
context, answered by exactly one model call, and the model's closed JSON *decision* is validated
before anything happens: a question needs an atomic social reservation, a promise exists only after
its utterance is DELIVERED, an action becomes a proposal for the behavior path (never run here).
A partial stream is only provisional text for the private panel; STOP/pause/epoch loss closes the
stream, drops the result and leaves no reservation, proposal, promise or memory of a spoken reply.
"""

import asyncio
import hashlib
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from ai_agent_contracts.api import MessageArguments, MessageCommand
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import ActionProposal
from ai_agent_contracts.states import ActivityState, DeliveryState, Provenance, RunState

from ai_agent_application.llm import GenerationRequest
from ai_agent_application.memory import EpisodeDraft, SourceDraft

from .attention import AttentionQueue, Offer, Selection
from .context import BuiltContext, ContextBuilder, Turn
from .extract import ReplyExtractor
from .initiative import InitiativeLimiter
from .model import (
    ContextManifest,
    ControlSnapshot,
    DialogueDecision,
    DialogueOutcome,
    DraftReply,
    Goal,
    Signal,
    SourceRights,
)
from .ports import ControlView, Generator, MemoryPort, PrivateStream, ProposalSink, SocialPort

QUESTION_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class ControllerPolicy:
    role_id: str = "character"
    operator_audience: str = "operator-panel"
    public_audience: str = "public-chat"
    proposal_ttl: timedelta = timedelta(minutes=10)
    obligation_lookahead: timedelta = timedelta(minutes=5)
    reminder_interval: timedelta = timedelta(hours=1)
    max_reminders: int = 3
    turn_memory: int = 12


@dataclass(frozen=True)
class Settled:
    pending: bool
    question: str | None = None
    promise_id: str | None = None
    episode_id: str | None = None


@dataclass
class _Pending:
    outcome: DialogueOutcome
    signal: Signal
    input_ref: SourceRef | None


@dataclass
class DialogueController:
    scope: Scope
    generator: Generator
    context: ContextBuilder
    social: SocialPort
    memory: MemoryPort
    control: ControlView
    clock: Callable[[], datetime]
    capabilities: Callable[[], Sequence[str]] = lambda: ()
    proposals: ProposalSink | None = None
    stream: PrivateStream | None = None
    attention: AttentionQueue = field(default_factory=AttentionQueue)
    limiter: InitiativeLimiter = field(default_factory=InitiativeLimiter)
    policy: ControllerPolicy = field(default_factory=ControllerPolicy)
    goal: Goal | None = None
    stats: Counter[str] = field(default_factory=Counter)
    _turns: deque[Turn] = field(default_factory=deque)
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _reminders: dict[str, tuple[datetime, int]] = field(default_factory=dict)
    _grounds_by_promise: dict[str, list[SourceRef]] = field(default_factory=dict)

    # ---- intake -----------------------------------------------------------------------

    def submit(self, signal: Signal) -> Offer:
        return self.attention.offer(signal, self.clock())

    def _open(self, snap: ControlSnapshot) -> bool:
        return (
            snap.admitted
            and snap.run_state is RunState.ACTIVE
            and snap.activity is ActivityState.OPEN
        )

    def _command(self, name: str) -> MessageCommand:
        return MessageCommand(
            request_id=name,
            scope=self.scope,
            target="dialogue",
            type="dialogue.message",
            arguments=MessageArguments(text="dialogue"),
        )

    # ---- one step ---------------------------------------------------------------------

    async def step(self, cancel: asyncio.Event | None = None) -> DialogueOutcome | None:
        """Handle at most one selected signal. Nothing is processed while control is closed."""
        snap = self.control.snapshot()
        if snap.run_state in (RunState.STOPPED, RunState.STOPPING, RunState.FAULTED):
            self.attention.clear()  # a stopped session never replays what queued up
            return None
        if not self._open(snap):
            return None
        selection = self.attention.select(self.clock(), self.goal)
        if selection is None:
            return None
        outcome = await self._process(selection, snap, cancel)
        self.attention.note_handled(selection.signal, self.clock())
        self.stats[outcome.status] += 1
        return outcome

    async def _process(
        self, selection: Selection, snap: ControlSnapshot, cancel: asyncio.Event | None
    ) -> DialogueOutcome:
        signal, event = selection.signal, selection.signal.event_id
        if not signal.rights.read:
            return DialogueOutcome(event, "blocked", "SOURCE_NOT_READABLE")
        initiative = signal.kind == "obligation_due"

        ref: SourceRef | None = None
        person_id = signal.person_id
        if not initiative:
            ref = await self._register(signal, self._command(f"src-{event}"))
            if signal.platform_id and signal.account_id:
                found = await self.social.observe_account(
                    self._command(f"acct-{event}"),
                    signal.platform_id,
                    signal.account_id,
                    signal.display_name or signal.account_id,
                    ref,
                )
                if found is not None:
                    person_id = found.person.id
                    self.limiter.answered(person_id)
                    await self.social.record_encounter(
                        self._command(f"enc-{event}"),
                        [person_id],
                        [ref],
                        f"{self.scope.session_id}:{person_id}",
                    )
        if snap.autonomy == "observation" and signal.kind != "operator_message":
            return DialogueOutcome(event, "silent", "OBSERVATION_MODE", person_id=person_id)
        if not signal.rights.api_transfer and not initiative:
            return DialogueOutcome(
                event, "blocked", "API_TRANSFER_NOT_ALLOWED", person_id=person_id
            )
        if initiative:
            if snap.autonomy != "autonomous" or person_id is None:
                return DialogueOutcome(event, "held", "INITIATIVE_NOT_ALLOWED")
            allowed, why = self.limiter.allowed(person_id, self.clock())
            if not allowed:
                return DialogueOutcome(event, "held", why, person_id=person_id)
            info = await self.social.context_for(self.scope, person_id)
            if info is None or info.contact != "normal":
                return DialogueOutcome(event, "held", "DO_NOT_CONTACT", person_id=person_id)

        built = await self.context.build(
            self.scope,
            signal,
            person_id=person_id,
            incoming=ref,
            turns=tuple(self._turns),
            capabilities=tuple(self.capabilities()),
        )
        if not initiative:
            self._remember_turn(Turn("person", signal.text, signal.display_name or "аноним"))
        return await self._decide(signal, built, ref, person_id, cancel, initiative)

    async def _register(self, signal: Signal, command: MessageCommand) -> SourceRef:
        result = await self.memory.register_source(
            command,
            SourceDraft(
                provenance=(
                    Provenance.SIMULATION
                    if self.scope.memory_mode == "test"
                    else Provenance.OPERATOR_INPUT
                    if signal.kind == "operator_message"
                    else Provenance.THIRD_PARTY_MESSAGE
                ),
                source_type=signal.kind.replace("_", "-"),
                adapter_id="dialogue",
                adapter_version="1",
                occurred_at=signal.occurred_at,
                content=signal.text,
                read_allowed=signal.rights.read,
                api_transfer_allowed=signal.rights.api_transfer,
                public_disclosure_allowed=signal.rights.public_disclosure,
                external_id=signal.event_id,
                author_id=None,
            ),
        )
        return SourceRef(
            source_id=result.source_id,
            version=result.version,
            scope=self.scope,
            availability="available",
        )

    # ---- model call and validation ----------------------------------------------------

    async def _decide(
        self,
        signal: Signal,
        built: BuiltContext,
        ref: SourceRef | None,
        person_id: str | None,
        cancel: asyncio.Event | None,
        initiative: bool,
    ) -> DialogueOutcome:
        event = signal.event_id
        local = asyncio.Event()
        forwarder = (
            asyncio.ensure_future(self._forward(cancel, local)) if cancel is not None else None
        )
        extractor = ReplyExtractor()
        private = self.stream is not None and signal.kind == "operator_message"

        async def on_delta(raw: str) -> None:
            if not self._open(self.control.snapshot()):
                local.set()  # STOP/pause: stop reading and showing at once
                return
            text = extractor.feed(raw)
            if text and private and self.stream is not None:
                await self.stream.emit(event, text)

        try:
            generation = await self.generator.generate(
                GenerationRequest(
                    operation_id=f"op-{event}",
                    role_id=self.policy.role_id,
                    messages=built.messages,
                    result_model=DialogueDecision,
                ),
                cancel=local,
                on_delta=on_delta,
            )
        finally:
            if forwarder is not None:
                forwarder.cancel()
        usages = tuple(a.usage for a in generation.attempts)
        base = {
            "event_id": event,
            "manifest": built.manifest,
            "attempts": len(generation.attempts),
            "usage": usages,
            "person_id": person_id,
        }
        if generation.status != "completed" or not isinstance(generation.parsed, DialogueDecision):
            if private and self.stream is not None:
                await self.stream.close(event, generation.status)
            status = "cancelled" if generation.status == "cancelled" else "failed"
            reason = generation.failure.value if generation.failure else "GENERATION_FAILED"
            return DialogueOutcome(status=status, reason=reason, **base)  # type: ignore[arg-type]
        decision = generation.parsed
        if not self._open(self.control.snapshot()):
            if private and self.stream is not None:
                await self.stream.close(event, "cancelled")
            return DialogueOutcome(status="cancelled", reason="CONTROL_CLOSED", **base)  # type: ignore[arg-type]
        return await self._apply(signal, decision, ref, person_id, initiative, base, private)

    @staticmethod
    async def _forward(source: asyncio.Event, target: asyncio.Event) -> None:
        await source.wait()
        target.set()

    async def _apply(
        self,
        signal: Signal,
        decision: DialogueDecision,
        ref: SourceRef | None,
        person_id: str | None,
        initiative: bool,
        base: dict[str, Any],
        private: bool,
    ) -> DialogueOutcome:
        event = signal.event_id
        dropped: list[str] = []
        now = self.clock()
        autonomous = self.control.snapshot().autonomy == "autonomous"

        proposals: list[ActionProposal] = []
        if decision.action is not None:
            action = decision.action
            if not autonomous:
                dropped.append("ACTION:OBSERVATION_MODE")
            elif action.capability_id not in set(self.capabilities()):
                dropped.append("ACTION:CAPABILITY_NOT_ALLOWED")
            elif ref is None and not self._grounds(signal):
                dropped.append("ACTION:NO_GROUNDS")
            else:
                grounds = [ref] if ref is not None else self._grounds(signal)
                proposals.append(
                    ActionProposal(
                        id=f"prop-{event}",
                        scope=self.scope,
                        created_at=now,
                        type="action.proposal",
                        goal=action.goal,
                        grounds=grounds,
                        expected_outcome=action.expected_outcome,
                        applicability=action.applicability,
                        priority=action.priority,
                        expires_at=now + self.policy.proposal_ttl,
                        capability_ids=[action.capability_id],
                    )
                )

        text = decision.reply or ""
        question_topic: str | None = None
        if decision.question is not None:
            if person_id is None:
                dropped.append("QUESTION:ANONYMOUS")
            else:
                reserved = await self.social.reserve_question(
                    self._command(f"qres-{event}"),
                    person_id,
                    decision.question.topic_key,
                    f"op-{event}",
                )
                if reserved.allowed:
                    question_topic = decision.question.topic_key
                    text = (text + QUESTION_SEPARATOR if text else "") + decision.question.text
                else:
                    dropped.append(f"QUESTION:{reserved.reason}")
        promise = decision.promise
        if promise is not None and (not decision.reply or person_id is None):
            dropped.append("PROMISE:NO_REPLY_OR_PERSON")
            promise = None

        # last look at control before anything leaves this method
        if not self._open(self.control.snapshot()):
            if question_topic is not None and person_id is not None:
                assert decision.question is not None
                await self.social.settle_question(
                    self._command(f"qrel-{event}"),
                    person_id,
                    question_topic,
                    f"op-{event}",
                    "not_delivered",
                )
            if private and self.stream is not None:
                await self.stream.close(event, "cancelled")
            return DialogueOutcome(
                status="cancelled", reason="CONTROL_CLOSED", dropped=tuple(dropped), **base
            )

        if proposals and self.proposals is not None:
            for proposal in proposals:
                await self.proposals.submit(proposal)
        if not text.strip():
            if private and self.stream is not None:
                await self.stream.close(event, "silent")
            reason = decision.silence_reason or ("PROPOSAL_ONLY" if proposals else "SILENT")
            return DialogueOutcome(
                status="silent",
                reason=reason,
                proposals=tuple(proposals),
                dropped=tuple(dropped),
                **base,
            )
        audience = (
            self.policy.operator_audience
            if signal.kind == "operator_message"
            else self.policy.public_audience
        )
        draft = DraftReply(
            utterance_id=f"utt-{event}",
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            audience_id=audience,
            person_id=person_id,
            question_topic=question_topic,
            promise=promise,
            evidence=tuple([ref] if ref is not None else self._grounds(signal)),
            initiative=initiative,
        )
        outcome = DialogueOutcome(
            status="drafted",
            draft=draft,
            proposals=tuple(proposals),
            dropped=tuple(dropped),
            **base,
        )
        self._pending[draft.utterance_id] = _Pending(outcome, signal, ref)
        if private and self.stream is not None:
            await self.stream.close(event, "drafted")
        return outcome

    def _grounds(self, signal: Signal) -> list[SourceRef]:
        """Sources behind an obligation signal: the promise's own exact evidence."""
        return list(self._grounds_by_promise.get(signal.promise_id or "", []))

    # ---- delivery -> memory, promises, questions --------------------------------------

    async def settle(self, utterance_id: str, status: DeliveryState, delivery_id: str) -> Settled:
        """Apply the *delivered* fact. Drafts, queued and sending replies change nothing."""
        pending = self._pending.get(utterance_id)
        if pending is None:
            return Settled(pending=True)
        if status in (DeliveryState.QUEUED, DeliveryState.SENDING, DeliveryState.PARTIAL):
            return Settled(pending=True)
        draft = pending.outcome.draft
        assert draft is not None
        del self._pending[utterance_id]
        person_id = draft.person_id
        question = None
        if draft.question_topic is not None and person_id is not None:
            result: Literal["delivered", "not_delivered", "unknown"] = "not_delivered"
            if status is DeliveryState.DELIVERED:
                result = "delivered"
            elif status is DeliveryState.UNKNOWN:
                result = "unknown"
            await self.social.settle_question(
                self._command(f"qset-{utterance_id}"),
                person_id,
                draft.question_topic,
                f"op-{pending.signal.event_id}",
                result,
                utterance_id=utterance_id,
            )
            question = result
        if status is DeliveryState.UNKNOWN and draft.initiative and person_id is not None:
            self.limiter.record(person_id, self.clock())  # an unknown delivery may have happened
        if status is not DeliveryState.DELIVERED:
            return Settled(pending=False, question=question)

        if draft.initiative and person_id is not None:
            self.limiter.record(person_id, self.clock())
        promise_id: str | None = None
        if draft.promise is not None and person_id is not None:
            now = self.clock()
            record, _ = await self.social.record_promise(
                self._command(f"prom-{utterance_id}"),
                [person_id],
                draft.promise.topic_key,
                delivery_id=delivery_id,
                utterance_id=utterance_id,
                promise_key=draft.promise.promise_key,
                content=draft.promise.content,
                completion_criterion=draft.promise.completion_criterion,
                sources=list(draft.evidence),
                deadline=None
                if draft.promise.deadline_days is None
                else now + timedelta(days=draft.promise.deadline_days),
            )
            promise_id = record.id
        episode = await self._record_episode(pending, draft, person_id)
        self._remember_turn(Turn("character", draft.text, "персонаж"))
        return Settled(False, question, promise_id, episode)

    async def _record_episode(
        self, pending: _Pending, draft: DraftReply, person_id: str | None
    ) -> str | None:
        if not draft.evidence:
            return None
        saying = await self.memory.register_source(
            self._command(f"out-{draft.utterance_id}"),
            SourceDraft(
                provenance=Provenance.SIMULATION
                if self.scope.memory_mode == "test"
                else Provenance.OBSERVATION,
                source_type="character-utterance",
                adapter_id="dialogue",
                adapter_version="1",
                occurred_at=self.clock(),
                content=draft.text,
                read_allowed=True,
                api_transfer_allowed=pending.signal.rights.api_transfer,
                public_disclosure_allowed=False,
                external_id=draft.utterance_id,
            ),
        )
        own = SourceRef(
            source_id=saying.source_id,
            version=saying.version,
            scope=self.scope,
            availability="available",
        )
        episode_id, _ = await self.memory.record_episode(
            self._command(f"epi-{draft.utterance_id}"),
            EpisodeDraft(
                sources=(*draft.evidence, own),
                participant_ids=(person_id,) if person_id else (),
                choice="Ответил собеседнику.",
                actual_outcome="Реплика доставлена адресату.",
                operation_id=f"op-{pending.signal.event_id}",
                provenance="simulation" if self.scope.memory_mode == "test" else "observation",
            ),
        )
        return str(episode_id)

    def _remember_turn(self, turn: Turn) -> None:
        self._turns.append(turn)
        while len(self._turns) > self.policy.turn_memory:
            self._turns.popleft()

    # ---- initiative from exact obligations --------------------------------------------

    async def collect_initiatives(self) -> list[str]:
        """Queue reminders for promises that are due: from the exact record, never from search."""
        snap = self.control.snapshot()
        if not self._open(snap) or snap.autonomy != "autonomous":
            return []
        now = self.clock()
        offered: list[str] = []
        for item in await self.social.obligations(self.scope):
            promise = item.promise
            if (
                promise.deadline is None
                or promise.deadline > now + self.policy.obligation_lookahead
            ):
                continue
            last = self._reminders.get(promise.id)
            if last is not None and (
                last[1] >= self.policy.max_reminders
                or now - last[0] < self.policy.reminder_interval
            ):
                continue
            person_id = promise.participant_ids[0]
            info = await self.social.context_for(self.scope, person_id)
            if info is None or info.contact != "normal":
                self.stats["initiative_blocked_contact"] += 1
                continue
            allowed, why = self.limiter.allowed(person_id, now)
            if not allowed:
                self.stats[f"initiative_blocked_{why}"] += 1
                continue
            count = 1 if last is None else last[1] + 1
            self._reminders[promise.id] = (now, count)
            event_id = f"obl-{promise.id}-{count}"
            self._grounds_by_promise[promise.id] = list(promise.sources)
            result = self.attention.offer(
                Signal(
                    event_id=event_id,
                    kind="obligation_due",
                    occurred_at=now,
                    text="Срок обещания наступил; реши, как уместно вернуться к нему.",
                    dedup_key=f"obligation:{promise.id}",
                    promise_id=promise.id,
                    person_id=person_id,
                ),
                now,
            )
            if result.result == "accepted":
                offered.append(event_id)
        return offered

    # ---- panel ------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "goal": None
            if self.goal is None
            else {
                "kind": self.goal.kind,
                "reason": self.goal.reason,
                "person": self.goal.person_id,
            },
            "pending_signals": self.attention.snapshot(),
            "awaiting_delivery": sorted(self._pending),
            "attention": {
                "accepted": self.attention.stats.accepted,
                "coalesced": self.attention.stats.coalesced,
                "dropped_budget": self.attention.stats.dropped_budget,
                "dropped_full": self.attention.stats.dropped_full,
                "expired": self.attention.stats.expired,
                "deferred": self.attention.stats.deferred,
            },
            "outcomes": dict(self.stats),
        }


__all__ = ["ContextManifest", "ControllerPolicy", "DialogueController", "Settled", "SourceRights"]
