"""Social owner: people, encounters, relationships, question topics and promises.

Identity is only ever a platform-confirmed ``(platform_id, account_id)`` inside one scope
(workspace / persona / memory space / work-test). Nicknames, wording and matching text never
merge or identify anything. Exact obligations (promises, known answers, reservations, contact
preferences) are read straight from the structured store, never through recall or any top-k.
Reads that need a lookup the repository cannot express (account -> person, person -> encounters)
scan the owner's own records with a per-scope cache that this service keeps current; that is valid
for the single core process of the MVP (see the task report for the storage request).
"""

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import Id, Record, Scope, SourceRef
from ai_agent_contracts.control import Delivery
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import (
    Encounter,
    Episode,
    Person,
    Promise,
    Relationship,
    Source,
    Topic,
)
from ai_agent_contracts.persona import StateChange
from ai_agent_contracts.states import ChangeState, DeliveryState, ErrorCode
from pydantic import TypeAdapter

from ai_agent_application.extraction.evidence import classify, experience_groups
from ai_agent_application.fencing import admission_guard, fenced
from ai_agent_application.storage import (
    Owner,
    Principal,
    RecordAccess,
    RecordRepository,
    Storage,
)

from .keys import derived_id, normalize_topic_key, person_id_for, topic_id_for
from .relationships import AspectLimit, RelationshipDelta
from .schemas import ASPECTS_SCHEMA, ContactPreference, RelationshipAspects

LIST_PAGE = 200
MAX_RETRIES = 3

type BlockReason = Literal[
    "ANSWER_KNOWN", "AWAITING_ANSWER", "RESERVED", "COOLDOWN", "DO_NOT_CONTACT", "CLOSED"
]


@dataclass(frozen=True)
class SocialPolicy:
    aspect_limits: tuple[AspectLimit, ...] = ()
    reservation_ttl: timedelta = timedelta(minutes=2)
    cooldown_after_question: timedelta = timedelta(minutes=10)
    unknown_delivery_hold: timedelta = timedelta(minutes=30)


@dataclass(frozen=True)
class Identified:
    person: Person
    created: bool
    renamed: bool


@dataclass(frozen=True)
class EncounterResult:
    encounter_id: str
    duplicate: bool
    first_meeting: bool
    previous_meetings: int


@dataclass(frozen=True)
class QuestionDecision:
    allowed: bool
    topic_id: str
    reason: BlockReason | None = None
    retry_after: datetime | None = None
    answer: str | None = None


@dataclass(frozen=True)
class Obligation:
    promise: Promise
    overdue: bool


@dataclass(frozen=True)
class PersonContext:
    person: Person
    relationship: Relationship | None
    contact: ContactPreference
    meetings: int
    first_met: datetime | None
    last_met: datetime | None
    returning: bool
    open_topics: tuple[Topic, ...]
    known_answers: tuple[Topic, ...]
    obligations: tuple[Obligation, ...]


class SocialService:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        extensions: ExtensionRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy: SocialPolicy | None = None,
        reader_access: tuple[RecordAccess, ...] = (),
        admitted: Callable[[], bool] = lambda: False,
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.SOCIAL:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.storage, self.principal, self.extensions = storage, principal, extensions
        self.clock, self.reader_access = clock, reader_access
        self.admitted = admitted
        self.policy = policy if policy is not None else SocialPolicy()
        self._accounts: dict[tuple[str, ...], dict[tuple[str, str], str]] = {}
        self._encounters: dict[tuple[str, ...], dict[str, list[Encounter]]] = {}

    def _write_fence(self) -> AbstractContextManager[None]:
        return self.storage.fence(admission_guard(self.admitted))

    # ---- plumbing -------------------------------------------------------------------------

    def _repo[R: Record](self, model: type[R], scope: Scope) -> RecordRepository[R]:
        return self.storage.repository(model, self.principal, scope)

    def _record[R: Any](self, model: type[R], payload: dict[str, Any]) -> R:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return from_wire(TypeAdapter(model), encoded, extensions=self.extensions)

    def _revised[R: Any](self, record: R, **changes: Any) -> R:
        data = json.loads(to_wire(record, extensions=self.extensions))
        data.update(changes)
        return self._record(type(record), data)

    def _dump(self, refs: Sequence[SourceRef]) -> list[dict[str, Any]]:
        return [ref.model_dump(mode="json") for ref in refs]

    def _stamp(self, scope: Scope) -> datetime:
        return self.clock()

    async def _write(
        self,
        command: CommandBase,
        puts: Sequence[tuple[Record, int | None]],
        resource_id: str,
        version: int,
    ) -> tuple[Receipt, bool]:
        """One owner transaction. Returns (receipt, replayed)."""
        async with self.storage.transaction(self.principal, command.scope, command) as unit:
            if unit.replay is not None:
                return unit.replay, True
            for record, expected in puts:
                await unit.put(record, expected_version=expected, access=self.reader_access or None)
            receipt = Receipt(
                request_id=command.request_id,
                scope=command.scope,
                status="completed",
                resource_id=resource_id,
                version=version,
            )
            await unit.complete(receipt)
            return receipt, False

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

    # ---- people ---------------------------------------------------------------------------

    async def _account_map(self, scope: Scope) -> dict[tuple[str, str], str]:
        loaded = self._accounts.get(scope.partition)
        if loaded is None:
            loaded = {}
            for person in await self._all(Person, scope):
                for link in person.accounts:
                    if link.confirmation == "confirmed":
                        loaded[link.platform_id, link.account_id] = person.id
            self._accounts[scope.partition] = loaded
        return loaded

    @staticmethod
    def _confirmed(person: Person, platform_id: str, account_id: str) -> bool:
        return any(
            link.platform_id == platform_id
            and link.account_id == account_id
            and link.confirmation == "confirmed"
            for link in person.accounts
        )

    async def identify(self, scope: Scope, platform_id: str, account_id: str) -> Person | None:
        """Resolve by confirmed platform account only. No account id -> no identity."""
        if not account_id.strip() or not platform_id.strip():
            return None
        repository = self._repo(Person, scope)
        anchor = await repository.get(person_id_for(scope, platform_id, account_id))
        if anchor is not None and self._confirmed(anchor, platform_id, account_id):
            return anchor
        owner_id = (await self._account_map(scope)).get((platform_id, account_id))
        if owner_id is None:
            return None
        person = await repository.get(owner_id)
        if person is not None and self._confirmed(person, platform_id, account_id):
            return person
        self._accounts.pop(scope.partition, None)  # stale entry: reload next time
        return None

    async def observe_account(
        self,
        command: CommandBase,
        platform_id: Id,
        account_id: str,
        display_name: str,
        evidence: SourceRef,
    ) -> Identified | None:
        """First sight creates the person; later sight returns the same one (name may change).

        Returns None for anonymous senders: no account id means no person and no history.
        """
        scope = command.scope
        for _ in range(MAX_RETRIES):
            person = await self.identify(scope, platform_id, account_id)
            now = self.clock()
            if person is None:
                if not account_id.strip():
                    return None
                person_id = person_id_for(scope, platform_id, account_id)
                created = self._record(
                    Person,
                    {
                        "id": person_id,
                        "scope": scope.model_dump(mode="json"),
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                        "version": 1,
                        "person_id": person_id,
                        "accounts": [self._link(platform_id, account_id, display_name, evidence)],
                    },
                )
                try:
                    await self._write(command, [(created, None)], person_id, 1)
                except ContractError as error:
                    if error.error.code is ErrorCode.VERSION_CONFLICT:
                        self._accounts.pop(scope.partition, None)
                        continue
                    raise
                self._accounts.setdefault(scope.partition, {})[platform_id, account_id] = person_id
                return Identified(created, True, False)
            current = next(
                link
                for link in person.accounts
                if (link.platform_id, link.account_id) == (platform_id, account_id)
                and link.confirmation == "confirmed"
            )
            if current.display_name == display_name:
                return Identified(person, False, False)
            links = [
                self._link(platform_id, account_id, display_name, evidence)
                if link is current
                else link.model_dump(mode="json")
                for link in person.accounts
            ]
            revised = self._revised(
                person,
                version=person.version + 1,
                updated_at=max(now, person.updated_at).isoformat(),
                accounts=links,
            )
            try:
                await self._write(command, [(revised, person.version)], person.id, revised.version)
            except ContractError as error:
                if error.error.code is ErrorCode.VERSION_CONFLICT:
                    continue
                raise
            return Identified(revised, False, True)
        raise ContractError(ErrorCode.RESOURCE_BUSY, command.request_id)

    @staticmethod
    def _link(
        platform_id: str,
        account_id: str,
        display_name: str,
        evidence: SourceRef | Mapping[str, Any],
        confirmation: str = "confirmed",
    ) -> dict[str, Any]:
        proof = evidence.model_dump(mode="json") if isinstance(evidence, SourceRef) else evidence
        return {
            "platform_id": platform_id,
            "account_id": account_id,
            "display_name": display_name,
            "confirmation": confirmation,
            "evidence": [proof],
        }

    async def link_account(
        self,
        command: CommandBase,
        person_id: str,
        expected_version: int,
        platform_id: Id,
        account_id: str,
        display_name: str,
        evidence: SourceRef,
        *,
        confirmation: Literal["confirmed", "hypothesis", "rejected"],
    ) -> Receipt:
        """Operator/user decision (authorised by the caller), never an LLM guess.

        A hypothesis does not identify anyone. Confirming an account that already has its own
        person would be a merge, which this module refuses; a rejected link frees the account.
        """
        scope = command.scope
        person = await self._repo(Person, scope).get(person_id)
        if person is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if person.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, person.version)
        owner = await self.identify(scope, platform_id, account_id)
        if confirmation == "confirmed" and owner is not None and owner.id != person.id:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)  # merge needed
        links = [
            link.model_dump(mode="json")
            for link in person.accounts
            if (link.platform_id, link.account_id) != (platform_id, account_id)
        ]
        kept = [link for link in links if link["confirmation"] == "confirmed"]
        existing = [
            link
            for link in person.accounts
            if (link.platform_id, link.account_id) == (platform_id, account_id)
        ]
        if confirmation != "confirmed" and existing and not kept:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)  # last identity
        proofs = [*(e.model_dump(mode="json") for link in existing for e in link.evidence)][-199:]
        entry = self._link(platform_id, account_id, display_name, evidence, confirmation)
        entry["evidence"] = [*proofs, entry["evidence"][0]]
        links.append(entry)
        now = self.clock()
        revised = self._revised(
            person,
            version=person.version + 1,
            updated_at=max(now, person.updated_at).isoformat(),
            accounts=links,
        )
        receipt, _ = await self._write(
            command, [(revised, person.version)], person.id, revised.version
        )
        self._accounts.pop(scope.partition, None)
        return receipt

    # ---- encounters -----------------------------------------------------------------------

    async def _encounter_map(self, scope: Scope) -> dict[str, list[Encounter]]:
        loaded = self._encounters.get(scope.partition)
        if loaded is None:
            loaded = {}
            for encounter in await self._all(Encounter, scope):
                for person_id in encounter.person_ids:
                    loaded.setdefault(person_id, []).append(encounter)
            self._encounters[scope.partition] = loaded
        return loaded

    async def meetings(self, scope: Scope, person_id: str) -> list[Encounter]:
        found = (await self._encounter_map(scope)).get(person_id, [])
        return sorted(found, key=lambda e: (e.created_at, e.id))

    async def record_encounter(
        self,
        command: CommandBase,
        person_ids: Sequence[str],
        sources: Sequence[SourceRef],
        key: str,
    ) -> EncounterResult:
        """One logical meeting per ``key`` (e.g. session + person): voice and chat of the same
        utterance, retries and redeliveries do not multiply it."""
        scope = command.scope
        encounter_id = derived_id("enc", scope, key)
        before = await self._encounter_map(scope)
        previous = max((len(before.get(p, [])) for p in person_ids), default=0)
        existing = await self._repo(Encounter, scope).get(encounter_id)
        if existing is not None:
            return EncounterResult(encounter_id, True, False, max(0, previous - 1))
        now = self.clock()
        record = self._record(
            Encounter,
            {
                "id": encounter_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "person_ids": list(person_ids),
                "sources": self._dump(sources),
            },
        )
        try:
            _, replayed = await self._write(command, [(record, None)], encounter_id, 1)
        except ContractError as error:
            if error.error.code is ErrorCode.VERSION_CONFLICT:
                return EncounterResult(encounter_id, True, False, previous)
            raise
        if not replayed:
            for person_id in person_ids:
                before.setdefault(person_id, []).append(record)
        return EncounterResult(encounter_id, replayed, previous == 0, previous)

    # ---- relationships --------------------------------------------------------------------

    def _relationship_id(self, scope: Scope, person_id: str) -> str:
        return derived_id("rel", scope, person_id)

    async def get_relationship(self, scope: Scope, person_id: str) -> Relationship | None:
        return await self._repo(Relationship, scope).get(self._relationship_id(scope, person_id))

    def aspects_of(self, relationship: Relationship) -> RelationshipAspects:
        if relationship.aspects.schema_id != ASPECTS_SCHEMA:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        parsed = self.extensions.validate(
            relationship.aspects.schema_id,
            relationship.aspects.schema_version,
            relationship.aspects.data,
        )
        assert isinstance(parsed, RelationshipAspects)
        return parsed

    def _aspects_payload(self, aspects: RelationshipAspects) -> dict[str, Any]:
        return {
            "schema_id": ASPECTS_SCHEMA,
            "schema_version": "1.0",
            "data": aspects.model_dump(mode="json"),
        }

    async def ensure_relationship(
        self, command: CommandBase, person_id: str, defaults: Mapping[str, float | str]
    ) -> Relationship:
        """Relationship of this persona to the person; starts from the profile defaults."""
        scope = command.scope
        if await self._repo(Person, scope).get(person_id) is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        existing = await self.get_relationship(scope, person_id)
        if existing is not None:
            return existing
        now = self.clock()
        rel_id = self._relationship_id(scope, person_id)
        record = self._record(
            Relationship,
            {
                "id": rel_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "version": 1,
                "person_id": person_id,
                "aspects": self._aspects_payload(RelationshipAspects(values=dict(defaults))),
                "change_ids": [],
                "grounds": [],
            },
        )
        try:
            await self._write(command, [(record, None)], rel_id, 1)
        except ContractError as error:
            if error.error.code is ErrorCode.VERSION_CONFLICT:
                found = await self.get_relationship(scope, person_id)
                if found is not None:
                    return found
            raise
        return record

    @fenced
    async def apply_aspects(
        self,
        command: CommandBase,
        person_id: str,
        expected_version: int,
        values: Mapping[str, float | str],
        grounds: Sequence[SourceRef],
        change_id: str,
        *,
        bounds: Mapping[str, tuple[float, float]] | None = None,
    ) -> Receipt:
        """Apply configured, per-experience bounds; audit and relationship have one owner/commit."""
        scope = command.scope
        audit_id = derived_id("relationship-change", scope, person_id, change_id)
        old = await self._repo(StateChange, scope).get(audit_id)
        if old is not None:
            previous = self.extensions.validate(
                old.patch.schema_id, old.patch.schema_version, old.patch.data
            )
            assert isinstance(previous, RelationshipDelta)
            if (
                dict(values) != previous.after
                or old.base_version != expected_version
                or {(g.source_id, g.version) for g in grounds}
                != {(g.source_id, g.version) for g in old.evidence}
            ):
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT, command.request_id)
            return Receipt(
                request_id=command.request_id,
                scope=scope,
                status="completed",
                resource_id=self._relationship_id(scope, person_id),
                version=old.new_version,
            )
        current = await self.get_relationship(scope, person_id)
        if current is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if current.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, current.version)
        configured = {limit.name: limit for limit in self.policy.aspect_limits}
        if not self.admitted() or not configured:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE, command.request_id)
        if not values or not grounds:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        episodes = await self._all(Episode, scope)
        group_by_source = experience_groups(episodes)
        expected = "simulation" if scope.memory_mode == "test" else "observation"
        groups: set[str] = set()
        for ref in grounds:
            source = await self._repo(Source, scope).get(ref.source_id)
            if (
                source is None
                or source.version != ref.version
                or not source.read_allowed
                or source.availability != "available"
                or ref.scope.partition != scope.partition
                or ref.source_id not in group_by_source
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            evidence = classify(source, scope)
            if not evidence.independent or evidence.reject:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            if not any(
                e.provenance == expected and any(r.source_id == ref.source_id for r in e.sources)
                for e in episodes
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            groups.add(group_by_source[ref.source_id])
        previous_patches: list[tuple[RelationshipDelta, StateChange]] = []
        for record in await self._all(StateChange, scope):
            if (
                record.status is ChangeState.APPLIED
                and record.patch.schema_id == "social.relationship-change"
            ):
                patch = self.extensions.validate(
                    record.patch.schema_id, record.patch.schema_version, record.patch.data
                )
                assert isinstance(patch, RelationshipDelta)
                if patch.person_id == person_id:
                    previous_patches.append((patch, record))
        aspects = self.aspects_of(current)
        merged = dict(aspects.values)
        before: dict[str, float] = {}
        after: dict[str, float] = {}
        for name, value in values.items():
            rule = configured.get(name)
            current_value = merged.get(name)
            if (
                rule is None
                or isinstance(value, (str, bool))
                or not isinstance(current_value, float)
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            low, high = rule.minimum, rule.maximum
            if bounds is not None:
                extra_low, extra_high = bounds.get(name, (low, high))
                low, high = max(low, extra_low), min(high, extra_high)
            spent = {
                group_by_source.get(ref.source_id, ref.source_id)
                for patch, audit in previous_patches
                if name in patch.after
                for ref in audit.evidence
            }
            count = len(groups - spent)
            delta = value - current_value
            if (
                not low <= value <= high
                or not count
                or delta == 0
                or delta > rule.max_increase * count + 1e-9
                or -delta > rule.max_decrease * count + 1e-9
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            before[name], after[name] = current_value, float(value)
            merged[name] = value
        now = max(self.clock(), current.updated_at)
        revised = self._revised(
            current,
            version=current.version + 1,
            updated_at=now.isoformat(),
            aspects=self._aspects_payload(
                RelationshipAspects(values=merged, contact=aspects.contact)
            ),
            change_ids=[*current.change_ids, change_id][-200:],
            grounds=[*self._dump(current.grounds), *self._dump(grounds)][-200:],
        )
        audit = self._record(
            StateChange,
            {
                "id": audit_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "version": 1,
                "proposal_id": change_id,
                "owner": "social",
                "base_version": current.version,
                "patch": {
                    "schema_id": "social.relationship-change",
                    "schema_version": "1.0",
                    "data": RelationshipDelta(
                        person_id=person_id,
                        before=before,
                        after=after,
                        episode_groups=sorted(groups),
                    ).model_dump(mode="json"),
                },
                "evidence": self._dump(grounds),
                "interpretation": "Проверенное изменение отношения по опыту.",
                "uncertainty": 0,
                "expected_manifestation": "Проверить поведение в следующей встрече.",
                "checks": ["per-experience-limit"],
                "status": "APPLIED",
                "new_version": revised.version,
            },
        )
        receipt, _ = await self._write(
            command, [(revised, current.version), (audit, None)], current.id, revised.version
        )
        return receipt

    async def set_contact_preference(
        self,
        command: CommandBase,
        person_id: str,
        expected_version: int,
        preference: ContactPreference,
        ground: SourceRef,
    ) -> Receipt:
        """The person's own stated wish (e.g. 'leave me alone'); outranks any cooldown."""
        scope = command.scope
        current = await self.get_relationship(scope, person_id)
        if current is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if current.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, current.version)
        aspects = self.aspects_of(current)
        now = self.clock()
        revised = self._revised(
            current,
            version=current.version + 1,
            updated_at=max(now, current.updated_at).isoformat(),
            aspects=self._aspects_payload(
                RelationshipAspects(values=aspects.values, contact=preference)
            ),
            grounds=[*self._dump(current.grounds), *self._dump([ground])][-200:],
        )
        receipt, _ = await self._write(
            command, [(revised, current.version)], current.id, revised.version
        )
        return receipt

    # ---- topics and question reservations --------------------------------------------------

    async def get_topic(self, scope: Scope, person_id: str, topic_key: str) -> Topic | None:
        return await self._repo(Topic, scope).get(topic_id_for(scope, person_id, topic_key))

    def _evaluate(
        self, topic: Topic | None, operation_id: str, now: datetime, topic_id: str
    ) -> QuestionDecision:
        if topic is None:
            return QuestionDecision(True, topic_id)
        if topic.status == "answered":
            return QuestionDecision(False, topic_id, "ANSWER_KNOWN", answer=topic.answer)
        if topic.status == "closed":
            return QuestionDecision(False, topic_id, "CLOSED")
        if topic.status == "awaiting_answer":
            return QuestionDecision(False, topic_id, "AWAITING_ANSWER")
        if topic.status == "reserved":
            if topic.reserved_by_operation_id == operation_id:
                return QuestionDecision(True, topic_id)
            until = topic.reservation_deadline
            if until is not None and until > now:
                return QuestionDecision(False, topic_id, "RESERVED", retry_after=until)
        if topic.cooldown_until is not None and topic.cooldown_until > now:
            return QuestionDecision(False, topic_id, "COOLDOWN", retry_after=topic.cooldown_until)
        return QuestionDecision(True, topic_id)

    async def reserve_question(
        self, command: CommandBase, person_id: str, topic_key: str, operation_id: Id
    ) -> QuestionDecision:
        """Check known answer / pending / cooldown / contact wish, then reserve atomically.

        Parallel tasks race on the topic version: exactly one reservation is granted.
        A blocked request writes nothing.
        """
        scope = command.scope
        topic_id = topic_id_for(scope, person_id, topic_key)
        relationship = await self.get_relationship(scope, person_id)
        if relationship is not None and self.aspects_of(relationship).contact != "normal":
            return QuestionDecision(False, topic_id, "DO_NOT_CONTACT")
        for _ in range(MAX_RETRIES):
            now = self.clock()
            topic = await self._repo(Topic, scope).get(topic_id)
            decision = self._evaluate(topic, operation_id, now, topic_id)
            if not decision.allowed or (
                topic is not None
                and topic.status == "reserved"
                and topic.reserved_by_operation_id == operation_id
            ):
                return decision
            deadline = now + self.policy.reservation_ttl
            fields = {
                "status": "reserved",
                "reserved_by_operation_id": operation_id,
                "reservation_deadline": deadline.isoformat(),
            }
            if topic is None:
                record = self._record(
                    Topic,
                    {
                        "id": topic_id,
                        "scope": scope.model_dump(mode="json"),
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                        "version": 1,
                        "topic_id": topic_id,
                        "participant_ids": [person_id],
                        "utterance_ids": [],
                        "sources": [],
                        **fields,
                    },
                )
                expected = None
            else:
                record = self._revised(
                    topic,
                    version=topic.version + 1,
                    updated_at=max(now, topic.updated_at).isoformat(),
                    **fields,
                )
                expected = topic.version
            try:
                await self._write(command, [(record, expected)], topic_id, record.version)
            except ContractError as error:
                if error.error.code is ErrorCode.VERSION_CONFLICT:
                    continue  # somebody else moved the topic: evaluate again
                raise
            return QuestionDecision(True, topic_id, retry_after=deadline)
        return QuestionDecision(False, topic_id, "RESERVED")

    async def settle_question(
        self,
        command: CommandBase,
        person_id: str,
        topic_key: str,
        operation_id: Id,
        outcome: Literal["delivered", "not_delivered", "unknown"],
        utterance_id: Id | None = None,
    ) -> Topic:
        """Turn the reservation into fact. A draft that was blocked never counts as asked;
        an unknown delivery keeps the topic held (no immediate re-ask); voice + chat of one
        utterance settle once."""
        scope = command.scope
        topic = await self.get_topic(scope, person_id, topic_key)
        if topic is None or topic.reserved_by_operation_id != operation_id:
            if (
                topic is not None
                and outcome == "delivered"
                and utterance_id is not None
                and utterance_id in topic.utterance_ids
            ):
                return topic  # second channel of the same utterance
            raise ContractError(ErrorCode.STALE_CONTEXT, command.request_id)
        now = self.clock()
        changes: dict[str, Any]
        if outcome == "delivered":
            if utterance_id is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            changes = {
                "status": "awaiting_answer",
                "utterance_ids": [*topic.utterance_ids, utterance_id][-200:],
                "cooldown_until": (now + self.policy.cooldown_after_question).isoformat(),
                "reserved_by_operation_id": None,
                "reservation_deadline": None,
            }
        elif outcome == "not_delivered":
            changes = {
                "status": "open",
                "reserved_by_operation_id": None,
                "reservation_deadline": None,
            }
        else:
            changes = {
                "reservation_deadline": (now + self.policy.unknown_delivery_hold).isoformat()
            }
        revised = self._revised(
            topic,
            version=topic.version + 1,
            updated_at=max(now, topic.updated_at).isoformat(),
            **changes,
        )
        await self._write(command, [(revised, topic.version)], topic.id, revised.version)
        return revised

    async def record_answer(
        self,
        command: CommandBase,
        person_id: str,
        topic_key: str,
        answer: str,
        sources: Sequence[SourceRef],
        *,
        expected_version: int | None = None,
    ) -> Topic:
        """Exact known answer. Overwriting an earlier answer needs its version (a correction)."""
        scope = command.scope
        if not sources or not answer.strip():
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        topic_id = topic_id_for(scope, person_id, topic_key)
        topic = await self._repo(Topic, scope).get(topic_id)
        now = self.clock()
        base: dict[str, Any] = {
            "status": "answered",
            "answer": answer,
            "reserved_by_operation_id": None,
            "reservation_deadline": None,
            "sources": self._dump(sources),
        }
        if topic is None:
            if expected_version is not None:
                raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id)
            record = self._record(
                Topic,
                {
                    "id": topic_id,
                    "scope": scope.model_dump(mode="json"),
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "version": 1,
                    "topic_id": topic_id,
                    "participant_ids": [person_id],
                    "utterance_ids": [],
                    **base,
                },
            )
            expected = None
        else:
            if topic.status == "answered" and expected_version != topic.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, topic.version)
            record = self._revised(
                topic,
                version=topic.version + 1,
                updated_at=max(now, topic.updated_at).isoformat(),
                **{**base, "sources": [*self._dump(topic.sources), *self._dump(sources)][-200:]},
            )
            expected = topic.version
        await self._write(command, [(record, expected)], topic_id, record.version)
        return record

    async def close_topic(
        self, command: CommandBase, person_id: str, topic_key: str, expected_version: int
    ) -> Topic:
        scope = command.scope
        topic = await self.get_topic(scope, person_id, topic_key)
        if topic is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if topic.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, topic.version)
        now = self.clock()
        revised = self._revised(
            topic,
            version=topic.version + 1,
            updated_at=max(now, topic.updated_at).isoformat(),
            status="closed",
            reserved_by_operation_id=None,
            reservation_deadline=None,
        )
        await self._write(command, [(revised, topic.version)], topic.id, revised.version)
        return revised

    async def reopen_topic(
        self, command: CommandBase, person_id: str, topic_key: str, expected_version: int
    ) -> Topic:
        """Explicit decision to allow asking again (new context or reminder policy).

        The cooldown set when the question was delivered keeps applying."""
        scope = command.scope
        topic = await self.get_topic(scope, person_id, topic_key)
        if topic is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if topic.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, topic.version)
        if topic.status not in {"awaiting_answer", "closed"}:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        now = self.clock()
        revised = self._revised(
            topic,
            version=topic.version + 1,
            updated_at=max(now, topic.updated_at).isoformat(),
            status="open",
            reserved_by_operation_id=None,
            reservation_deadline=None,
        )
        await self._write(command, [(revised, topic.version)], topic.id, revised.version)
        return revised

    # ---- promises -------------------------------------------------------------------------

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
    ) -> tuple[Promise, bool]:
        """A promise exists only because a DELIVERED utterance of the agent said it.

        An LLM draft, a blocked or merely queued reply creates nothing. Voice and chat of the same
        utterance share one promise (keyed by utterance + promise key).
        Returns (promise, duplicate).
        """
        scope = command.scope
        delivery = await self._repo(Delivery, scope).get(delivery_id)
        if (
            delivery is None
            or delivery.utterance_id != utterance_id
            or delivery.status is not DeliveryState.DELIVERED
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if not participant_ids or not sources:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        promise_id = derived_id("promise", scope, utterance_id, normalize_topic_key(promise_key))
        existing = await self._repo(Promise, scope).get(promise_id)
        if existing is not None:
            return existing, True
        now = self.clock()
        record = self._record(
            Promise,
            {
                "id": promise_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "version": 1,
                "participant_ids": list(participant_ids),
                "topic_id": topic_id_for(scope, participant_ids[0], topic_key),
                "delivered_utterance_id": utterance_id,
                "delivery_id": delivery_id,
                "sources": self._dump(sources),
                "content": content,
                "deadline": deadline.isoformat() if deadline else None,
                "completion_criterion": completion_criterion,
                "status": "open",
            },
        )
        try:
            await self._write(command, [(record, None)], promise_id, 1)
        except ContractError as error:
            if error.error.code is ErrorCode.VERSION_CONFLICT:
                found = await self._repo(Promise, scope).get(promise_id)
                if found is not None:
                    return found, True
            raise
        return record, False

    async def close_promise(
        self,
        command: CommandBase,
        promise_id: str,
        expected_version: int,
        outcome: Literal["fulfilled", "withdrawn", "unknown"],
        closure_result: str | None,
        evidence: SourceRef | None = None,
    ) -> Promise:
        """Fulfilment needs its own evidence; a deleted conversation is not a fulfilment."""
        scope = command.scope
        promise = await self._repo(Promise, scope).get(promise_id)
        if promise is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if promise.version != expected_version:
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, promise.version)
        if promise.status in {"fulfilled", "withdrawn"}:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if outcome == "fulfilled" and (evidence is None or not closure_result):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if outcome == "withdrawn" and not closure_result:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        now = self.clock()
        sources = [*self._dump(promise.sources), *(self._dump([evidence]) if evidence else [])]
        revised = self._revised(
            promise,
            version=promise.version + 1,
            updated_at=max(now, promise.updated_at).isoformat(),
            status=outcome,
            closure_result=closure_result,
            sources=sources[-200:],
        )
        await self._write(command, [(revised, promise.version)], promise.id, revised.version)
        return revised

    async def obligations(self, scope: Scope, person_id: str | None = None) -> list[Obligation]:
        """Every open or unverified promise, exact and complete (no ranking, no limit)."""
        now = self.clock()
        found = [
            Obligation(p, p.deadline is not None and p.deadline < now)
            for p in await self._all(Promise, scope)
            if p.status in {"open", "unknown"}
            and (person_id is None or person_id in p.participant_ids)
        ]
        return sorted(
            found,
            key=lambda o: (o.promise.deadline or datetime.max.replace(tzinfo=UTC), o.promise.id),
        )

    # ---- context ---------------------------------------------------------------------------

    async def context_for(self, scope: Scope, person_id: str) -> PersonContext | None:
        """Everything exact the controller needs when this person returns."""
        person = await self._repo(Person, scope).get(person_id)
        if person is None:
            return None
        relationship = await self.get_relationship(scope, person_id)
        contact = self.aspects_of(relationship).contact if relationship else "normal"
        meetings = await self.meetings(scope, person_id)
        topics = [t for t in await self._all(Topic, scope) if person_id in t.participant_ids]
        return PersonContext(
            person=person,
            relationship=relationship,
            contact=contact,
            meetings=len(meetings),
            first_met=meetings[0].created_at if meetings else None,
            last_met=meetings[-1].created_at if meetings else None,
            returning=len(meetings) > 0,
            open_topics=tuple(t for t in topics if t.status in {"reserved", "awaiting_answer"}),
            known_answers=tuple(t for t in topics if t.status == "answered"),
            obligations=tuple(await self.obligations(scope, person_id)),
        )
