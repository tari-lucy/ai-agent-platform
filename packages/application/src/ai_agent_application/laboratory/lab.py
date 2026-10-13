"""Laboratory world: the real owners in a TEST scope, a scripted mock model, virtual time.

Everything here is isolated from work data: the scope must be ``memory_mode == "test"``, the model
is a deterministic mock that has to be scripted call by call (an unscripted call is a finding), and
time only moves through the virtual clock. A green mock scenario checks interfaces, invariants and
failure handling; it says nothing about the quality of a real LLM.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_agent_contracts.api import CreateCommand, MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Claim, Episode, Person, Promise, Source, Topic
from ai_agent_contracts.persona import CharacterState, Opportunity, Profile, StateChange, StoryArc
from ai_agent_contracts.states import ActivityState, DeliveryState, ErrorCode, RunState
from pydantic import TypeAdapter

from ai_agent_application.development import (
    DevelopmentController,
    MemoryEvidence,
    Reflector,
    register_development_extensions,
)
from ai_agent_application.dialogue import (
    ContextBuilder,
    ControlSnapshot,
    DialogueController,
    DialogueOutcome,
    Signal,
    SourceRights,
)
from ai_agent_application.dialogue.model import Autonomy
from ai_agent_application.extraction import Extractor
from ai_agent_application.gamemaster import Dice, Family, GameMaster, StoryRules
from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    GenerationParameters,
    LLMGateway,
    ProviderCall,
    ProviderError,
    ResolvedAssignment,
    Started,
)
from ai_agent_application.llm.model import ProviderEvent
from ai_agent_application.memory import IndexHealth, MemoryService, RecallService
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.persona import PersonaService, register_persona_extensions
from ai_agent_application.social import SocialService, register_social_extensions
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess, Storage

from .clock import VirtualClock
from .report import Finding

MEMORY = Principal("lab-memory", "core_owner", Owner.MEMORY)
SOCIAL = Principal("lab-social", "core_owner", Owner.SOCIAL)
CHANNEL = Principal("lab-channel", "core_owner", Owner.CHANNEL)
PERSONA = Principal("lab-persona", "core_owner", Owner.PERSONA)
GM = Principal("lab-gm", "core_owner", Owner.GM)
LABORATORY = Principal("lab-laboratory", "core_owner", Owner.LABORATORY)
PRINCIPALS = (MEMORY, SOCIAL, CHANNEL, PERSONA, GM, LABORATORY)
FAMILIES = (
    Family("find", "Необычная находка", ("explore",), ("камень", "след"), (0.2, 0.6)),
    Family("promise", "Вернуться к обещанию", ("talk",), ("напомнить",), (0.1, 0.3)),
)


def lab_grants() -> tuple[Grant, ...]:
    """Grants a laboratory scope needs; the trusted composition provisions them, not the lab."""
    return (
        Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True),
        Grant(SOCIAL, Owner.SOCIAL, read=True, write=True),
        Grant(SOCIAL, Owner.MEMORY, read=True),
        Grant(SOCIAL, Owner.CHANNEL, read=True),
        Grant(CHANNEL, Owner.CHANNEL, read=True, write=True),
        Grant(PERSONA, Owner.PERSONA, read=True, write=True),
        Grant(PERSONA, Owner.MEMORY, read=True),
        Grant(GM, Owner.GM, read=True, write=True),
        Grant(GM, Owner.MEMORY, read=True),
        Grant(LABORATORY, Owner.LABORATORY, read=True, write=True),
    )


@dataclass(frozen=True)
class Visitor:
    """A virtual participant. Two visitors may share a nickname and differ in account."""

    name: str
    account: str
    platform: str = "lab-chat"


class LabControl:
    """Supervisor stand-in the scenario can flip (STOP, pause, observation)."""

    def __init__(self) -> None:
        self.run_state, self.activity = RunState.ACTIVE, ActivityState.OPEN
        self.autonomy: Autonomy = "autonomous"
        self.admitted = True

    def snapshot(self) -> ControlSnapshot:
        return ControlSnapshot(self.run_state, self.activity, self.autonomy, self.admitted)

    def __call__(self) -> ControlSnapshot:
        return self.snapshot()


class LabBrain:
    """The mock model. Calls must be scripted; the lab reports any call that was not expected."""

    def __init__(self) -> None:
        self.queues: dict[str, list[list[ProviderEvent] | ProviderError]] = {}
        self.calls: list[tuple[str, str]] = []
        self.unscripted: list[str] = []

    def expect(self, role: str, events: list[ProviderEvent] | ProviderError) -> None:
        self.queues.setdefault(role, []).append(events)

    def expect_json(self, role: str, body: dict[str, Any]) -> None:
        text = json.dumps(body, ensure_ascii=False)
        events: list[ProviderEvent] = [Started("lab-mock")]
        events += [Delta(text[i : i + 17]) for i in range(0, len(text), 17)]
        events.append(Finished("stop"))
        self.expect(role, events)

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        role = call.assignment.role_id
        prompt = "\n".join(m.content for m in call.messages)
        self.calls.append((role, prompt))
        queue = self.queues.get(role, [])
        if not queue:
            self.unscripted.append(role)
            return self._play(ProviderError(Failure.REJECTED))
        return self._play(queue.pop(0))

    async def _play(
        self, script: list[ProviderEvent] | ProviderError
    ) -> AsyncIterator[ProviderEvent]:
        if isinstance(script, ProviderError):
            raise script
        for event in script:
            yield event
            await asyncio.sleep(0)


def assignment(role: str) -> ResolvedAssignment:
    return ResolvedAssignment(
        assignment_id=f"lab-{role}",
        assignment_version=1,
        role_id=role,
        connection_id="lab-mock",
        adapter_id="mock",
        model_id=f"lab-mock-{role}",
        endpoint_setting="AI_PLATFORM_LAB_ENDPOINT",
        credential_setting="AI_PLATFORM_LAB_KEY",
        parameters=GenerationParameters(max_output_tokens=800, timeout_seconds=30),
        capability_verification="unverified",
    )


@dataclass
class Lab:
    storage: Storage
    scope: Scope
    clock: VirtualClock
    brain: LabBrain
    registry: ExtensionRegistry
    memory: MemoryService
    recall: RecallService
    social: SocialService
    persona: PersonaService
    gm: GameMaster
    development: DevelopmentController
    reflector: Reflector
    extractor: Extractor
    control: LabControl
    generator: LLMGateway
    findings: list[Finding] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    counter: int = 0
    session_no: int = 1
    outputs_while_stopped: int = 0
    controller: DialogueController | None = None
    proposals: list[Any] = field(default_factory=list)
    stream_text: dict[str, str] = field(default_factory=dict)

    # ---- construction -----------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        storage: Storage,
        scope: Scope,
        *,
        seed: str,
        index: RecallIndex,
        profile_markdown: str,
        rules: StoryRules,
        clock: VirtualClock | None = None,
    ) -> Lab:
        if scope.memory_mode != "test":
            raise ContractError(ErrorCode.ACCESS_DENIED)  # the laboratory never touches work data
        clock = clock or VirtualClock()
        registry = ExtensionRegistry()
        register_persona_extensions(registry)
        register_social_extensions(registry)
        register_development_extensions(registry)
        brain = LabBrain()
        control = LabControl()
        access = tuple(RecordAccess(p.id) for p in (SOCIAL, GM, PERSONA))
        health = IndexHealth()
        memory = MemoryService(
            storage, MEMORY, registry, index=index, health=health, clock=clock, reader_access=access
        )
        recall = RecallService(storage, MEMORY, index, health=health, clock=clock)
        social = SocialService(storage, SOCIAL, registry, clock=clock, reader_access=access)
        persona = PersonaService(storage, PERSONA, registry, clock=clock)
        generator = LLMGateway(
            lambda _a: brain,
            lambda role: assignment(role),
            admission=lambda: control.snapshot().admitted,
            clock=clock,
            sleep=clock.sleep,
            jitter=lambda: 1.0,
        )
        gm = GameMaster(
            storage=storage,
            principal=GM,
            extensions=registry,
            memory=memory,
            rules=rules,
            families=FAMILIES,
            dice=Dice(seed),
            admitted=lambda: control.snapshot().admitted,
            clock=clock,
            reader_access=access,
        )
        development = DevelopmentController(
            storage=storage,
            principal=PERSONA,
            extensions=registry,
            evidence=MemoryEvidence(memory),
            control=control,
            rules=rules,
            clock=clock,
            reader_access=access,
        )
        reflector = Reflector(development, generator, clock=clock)
        extractor = Extractor(memory, generator, admitted=lambda: control.snapshot().admitted)
        lab = cls(
            storage,
            scope,
            clock,
            brain,
            registry,
            memory,
            recall,
            social,
            persona,
            gm,
            development,
            reflector,
            extractor,
            control,
            generator,
        )
        await lab._create_profile(profile_markdown)
        lab.controller = lab._new_controller()
        return lab

    async def _create_profile(self, markdown: str) -> None:
        payload = {
            "request_id": "lab-create-profile",
            "scope": self.scope.model_dump(mode="json"),
            "target": self.scope.persona_id,
            "type": "persona.create",
            "arguments": {
                "schema_id": "persona.create-args",
                "schema_version": "1.0",
                "data": {"markdown": markdown},
            },
        }
        command = from_wire(
            TypeAdapter(CreateCommand), json.dumps(payload).encode(), extensions=self.registry
        )
        await self.persona.create(command)

    def _session_scope(self) -> Scope:
        return self.scope.model_copy(
            update={
                "agent_id": "lab-agent",
                "run_id": "lab-run",
                "session_id": f"lab-s{self.session_no}",
            }
        )

    def _new_controller(self) -> DialogueController:
        scope = self._session_scope()

        class Sink:
            def __init__(self, lab: Lab) -> None:
                self.lab = lab

            async def submit(self, proposal: Any) -> None:
                self.lab.proposals.append(proposal)

        class Stream:
            def __init__(self, lab: Lab) -> None:
                self.lab = lab

            async def emit(self, event_id: str, text: str) -> None:
                self.lab.stream_text[event_id] = self.lab.stream_text.get(event_id, "") + text

            async def close(self, event_id: str, status: str) -> None:
                self.lab.note("stream_closed", event=event_id, status=status)

        builder = ContextBuilder(self.persona, self.social, self.recall, self.memory)
        return DialogueController(
            scope=scope,
            generator=self.generator,
            context=builder,
            social=self.social,
            memory=self.memory,
            control=self.control,
            clock=self.clock,
            capabilities=lambda: ("explore", "talk"),
            proposals=Sink(self),
            stream=Stream(self),
        )

    # ---- bookkeeping ------------------------------------------------------------------

    def note(self, kind: str, **data: Any) -> None:
        self.log.append({"t": self.clock().isoformat(), "kind": kind, **data})

    def check(self, name: str, condition: bool, detail: str = "", *, hard: bool = True) -> bool:
        self.findings.append(Finding(name, bool(condition), "" if condition else detail, hard))
        self.note("check", name=name, ok=bool(condition))
        return bool(condition)

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def command(self, scope: Scope | None = None, name: str | None = None) -> MessageCommand:
        return MessageCommand(
            request_id=name or self.next_id("lab-cmd"),
            scope=scope or self._session_scope(),
            target="laboratory",
            type="dialogue.message",
            arguments=MessageArguments(text="laboratory"),
        )

    # ---- participants and time --------------------------------------------------------

    def signal(
        self, visitor: Visitor | None, text: str, *, directed: bool = True, api: bool = True
    ) -> Signal:
        if visitor is None:  # the operator in the private panel
            return Signal(
                event_id=self.next_id("lab-evt"),
                kind="operator_message",
                occurred_at=self.clock(),
                text=text,
                directed=True,
                rights=SourceRights(read=True, api_transfer=True),
            )
        return Signal(
            event_id=self.next_id("lab-evt"),
            kind="viewer_message",
            occurred_at=self.clock(),
            text=text,
            platform_id=visitor.platform,
            account_id=visitor.account,
            display_name=visitor.name,
            directed=directed,
            rights=SourceRights(read=True, api_transfer=api),
        )

    async def say(
        self,
        visitor: Visitor | None,
        text: str,
        decision: dict[str, Any] | None = None,
        *,
        deliver: bool = True,
        directed: bool = True,
    ) -> DialogueOutcome | None:
        """A visitor writes; the mock model answers with ``decision``; the reply is delivered."""
        assert self.controller is not None
        if decision is not None:
            self.brain.expect_json("character", decision)
        self.controller.submit(self.signal(visitor, text, directed=directed))
        outcome = await self.controller.step()
        stopped = self.control.run_state is not RunState.ACTIVE
        if outcome is not None and outcome.draft is not None and stopped:
            self.outputs_while_stopped += 1
        self.note(
            "say",
            who="operator" if visitor is None else visitor.name,
            account="" if visitor is None else visitor.account,
            status=None if outcome is None else outcome.status,
            reason=None if outcome is None else outcome.reason,
            reply=None if outcome is None or outcome.draft is None else outcome.draft.text,
        )
        if deliver and outcome is not None and outcome.draft is not None:
            await self.deliver(outcome)
        return outcome

    async def deliver(self, outcome: DialogueOutcome, status: str = "DELIVERED") -> Any:
        """The channel owner's fact, then the controller's settle (questions, promises, memory)."""
        assert self.controller is not None and outcome.draft is not None
        delivery_id = self.next_id("lab-dlv")
        record = Delivery.model_validate(
            {
                "id": delivery_id,
                "scope": self.scope.model_dump(),
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "version": 1,
                "utterance_id": outcome.draft.utterance_id,
                "revision": 1,
                "channel_id": "lab-channel",
                "attempt_id": f"attempt-{delivery_id}",
                "status": DeliveryState(status),
            }
        )
        command = self.command(self.scope, self.next_id("lab-dcmd"))
        async with self.storage.transaction(CHANNEL, self.scope, command) as unit:
            await unit.put(record, access=(RecordAccess(SOCIAL.id),))
            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=self.scope,
                    status="completed",
                    resource_id=delivery_id,
                    version=1,
                )
            )
        settled = await self.controller.settle(
            outcome.draft.utterance_id, DeliveryState(status), delivery_id
        )
        self.note("delivered", utterance=outcome.draft.utterance_id, status=status)
        return settled

    async def advance(self, **delta: float) -> None:
        await self.clock.advance(**delta)
        self.note("advance", **delta)

    async def new_session(self) -> DialogueController:
        """A later session: a fresh controller over the same stores (state lives in the stores)."""
        self.session_no += 1
        self.controller = self._new_controller()
        self.note("new_session", session=self.session_no)
        return self.controller

    def stop(self) -> None:
        self.control.run_state, self.control.admitted = RunState.STOPPED, False
        self.note("stop")

    async def restart(self) -> None:
        """Process restart: the agent comes back STOPPED and does nothing until resumed."""
        self.session_no += 1
        self.controller = self._new_controller()
        self.control.run_state, self.control.admitted = RunState.STOPPED, False
        self.note("restart")

    def resume(self) -> None:
        self.control.run_state, self.control.admitted = RunState.ACTIVE, True
        self.note("resume")

    async def source(self, text: str, **kwargs: Any) -> SourceRef:
        from ai_agent_contracts.states import Provenance

        from ai_agent_application.memory import SourceDraft

        made = await self.memory.register_source(
            self.command(),
            SourceDraft(
                provenance=kwargs.get("provenance", Provenance.SIMULATION),
                source_type=kwargs.get("source_type", "lab-event"),
                adapter_id="laboratory",
                adapter_version="1",
                occurred_at=self.clock(),
                content=text,
                read_allowed=True,
                api_transfer_allowed=kwargs.get("api", True),
                public_disclosure_allowed=False,
                external_id=self.next_id("lab-src"),
                author_id=kwargs.get("author"),
            ),
        )
        return SourceRef(
            source_id=made.source_id,
            version=made.version,
            scope=self.scope,
            availability="available",
        )

    async def episode(self, text: str, participants: Sequence[str] = ()) -> str:
        """One lived (simulated) experience: a first-hand source and an episode over it."""
        from ai_agent_application.memory import EpisodeDraft

        ref = await self.source(text)
        episode_id, _ = await self.memory.record_episode(
            self.command(),
            EpisodeDraft(
                sources=(ref,),
                participant_ids=tuple(participants),
                choice="Выбор персонажа.",
                actual_outcome="Фактический исход сцены.",
                operation_id=self.next_id("lab-op"),
                provenance="simulation",
            ),
        )
        self.note("episode", text=text)
        return str(episode_id)

    # ---- inspection -------------------------------------------------------------------

    async def fingerprint(self, scope: Scope) -> dict[str, int]:
        """Record counts and version sums per kind, to prove another scope was not touched."""
        kinds: Sequence[tuple[Any, Principal]] = (
            (Source, MEMORY),
            (Episode, MEMORY),
            (Claim, MEMORY),
            (Person, SOCIAL),
            (Topic, SOCIAL),
            (Promise, SOCIAL),
            (Profile, PERSONA),
            (CharacterState, PERSONA),
            (StateChange, PERSONA),
            (StoryArc, GM),
            (Opportunity, GM),
        )
        result: dict[str, int] = {}
        for model, principal in kinds:
            try:
                rows = await self.storage.repository(model, principal, scope).list(limit=200)
            except ContractError:
                rows = []
            result[model.__name__] = len(rows) * 1000 + sum(getattr(r, "version", 1) for r in rows)
        return result

    async def rows(self, model: Any, principal: Principal, scope: Scope | None = None) -> list[Any]:
        return list(
            await self.storage.repository(model, principal, scope or self.scope).list(limit=200)
        )


Hook = Callable[[Lab], Any]
__all__ = ["Lab", "LabBrain", "LabControl", "Visitor", "lab_grants"]
