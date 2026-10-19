"""Memory owner: source / episode / claim lifecycle over the M1-02 repositories.

Authority is the transactional store. The recall index is updated after commit and is
reconcilable; a failed index update never rolls back a committed source and is made visible
through ``IndexHealth``. Retention TTL is counted from the original occurrence time, so retelling
or re-indexing never extends it. Content is never matched by text for identity: only the
external source identity (or an explicit episode) says two records are the same experience.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_agent_contracts.api import CommandBase, EditCommand, Receipt
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import Id, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Claim, Episode, MemoryCorrection, Source, Summary
from ai_agent_contracts.panel import MemoryCorrectArguments
from ai_agent_contracts.states import ErrorCode, Provenance
from pydantic import TypeAdapter

from ai_agent_application.storage import Owner, Principal, ReadPurpose, RecordAccess, Storage

from .chunking import MAX_CHUNK_CHARS, chunk_text
from .health import IndexHealth
from .ports import ClaimLink, Embedder, IndexedChunk, IndexedSource, RecallIndex

DEFAULT_TTL = timedelta(days=30)
POLICY_ID = "conversation-30d"
FUTURE_SKEW = timedelta(minutes=5)
EMBED_BATCH = 4
LIST_PAGE = 200


def derived_id(prefix: str, scope: Scope, *parts: str) -> str:
    material = "\0".join([*scope.partition, *parts]).encode("utf-8")
    return prefix + "-" + hashlib.sha256(material).hexdigest()[:40]


@dataclass(frozen=True)
class SourceDraft:
    """All three rights are explicit: no default silently opens a source to an API or the public."""

    provenance: Provenance
    source_type: Id
    adapter_id: Id
    adapter_version: Id
    occurred_at: datetime
    content: str
    read_allowed: bool
    api_transfer_allowed: bool
    public_disclosure_allowed: bool
    external_id: str | None = None
    author_id: Id | None = None
    identity_confidence: float | None = None
    protected: bool = False


@dataclass(frozen=True)
class RegisterResult:
    source_id: str
    version: int
    duplicate: bool
    replayed: bool
    indexed: bool


@dataclass(frozen=True)
class EpisodeDraft:
    sources: tuple[SourceRef, ...]
    participant_ids: tuple[Id, ...]
    choice: str
    actual_outcome: str
    operation_id: Id
    provenance: str
    interpretation: str | None = None
    experience_source_ids: tuple[Id, ...] = ()


@dataclass(frozen=True)
class ClaimDraft:
    claim_id: Id
    subject_id: Id
    content: str
    knowledge_type: str
    sources: tuple[SourceRef, ...]
    confidence: float
    conflicts_with: tuple[Id, ...] = ()


@dataclass(frozen=True)
class ReconcileReport:
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    extra: tuple[str, ...]
    rebuilt: bool


class MemoryService:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        extensions: ExtensionRegistry,
        *,
        index: RecallIndex | None = None,
        embedder: Embedder | None = None,
        health: IndexHealth | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = DEFAULT_TTL,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        reader_access: tuple[RecordAccess, ...] = (),
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.MEMORY:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if not timedelta(0) < ttl <= timedelta(days=3650):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self.storage, self.principal, self.extensions = storage, principal, extensions
        self.index, self.embedder, self.clock, self.ttl = index, embedder, clock, ttl
        self.health = health if health is not None else IndexHealth()
        self.max_chunk_chars = max_chunk_chars
        self.reader_access = reader_access

    # ---- helpers --------------------------------------------------------------------------

    async def get_source(
        self, scope: Scope, source_id: str, *, purpose: ReadPurpose = "private"
    ) -> Source | None:
        return await self.storage.repository(Source, self.principal, scope).get(
            source_id, purpose=purpose
        )

    def _record[R: Any](self, model: type[R], payload: dict[str, Any]) -> R:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return from_wire(TypeAdapter(model), encoded, extensions=self.extensions)

    def _revised[R: Any](self, record: R, **changes: Any) -> R:
        data = json.loads(to_wire(record, extensions=self.extensions))
        data.update(changes)
        return self._record(type(record), data)

    async def _embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        if self.embedder is None or not texts:
            return None
        try:
            vectors: list[list[float]] = []
            for start in range(0, len(texts), EMBED_BATCH):
                vectors.extend(await self.embedder.embed(texts[start : start + EMBED_BATCH]))
            ok = len(vectors) == len(texts) and all(
                len(vector) == self.embedder.dimension for vector in vectors
            )
            return vectors if ok else None
        except Exception:
            return None  # degraded to lexical-only; visible to recall as index_state

    async def _entry(self, source: Source) -> tuple[IndexedSource, str | None]:
        chunks = chunk_text(source.content or "", self.max_chunk_chars)
        vectors = await self._embed([chunk.text for chunk in chunks])
        indexed = tuple(
            IndexedChunk(chunk, tuple(vectors[number]) if vectors else None)
            for number, chunk in enumerate(chunks)
        )
        entry = IndexedSource(source.id, source.version, source.retention.expires_at, indexed)
        return entry, (self.embedder.signature if vectors and self.embedder else None)

    async def _index_source(self, scope: Scope, source: Source) -> bool:
        if self.index is None:
            return False
        try:
            entry, signature = await self._entry(source)
            await self.index.put_source(scope, entry, signature)
        except Exception:
            self.health.mark(scope, source.id)
            return False
        self.health.clear(scope, source.id)
        return True

    async def _unindex_source(self, scope: Scope, source_id: str) -> None:
        if self.index is None:
            return
        try:
            await self.index.remove_source(scope, source_id)
        except Exception:
            self.health.mark(scope, source_id)

    # ---- source lifecycle -----------------------------------------------------------------

    async def register_source(self, command: CommandBase, draft: SourceDraft) -> RegisterResult:
        scope = command.scope
        now = self.clock()
        if not draft.content.strip() or len(draft.content) > 16000:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if draft.occurred_at.utcoffset() != timedelta(0):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        if draft.occurred_at > now + FUTURE_SKEW:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        expires = None if draft.protected else draft.occurred_at + self.ttl
        if expires is not None and expires <= now:
            raise ContractError(ErrorCode.EXPIRED, command.request_id)
        if draft.external_id is not None:
            source_id = derived_id(
                "src", scope, "external", draft.source_type, draft.adapter_id, draft.external_id
            )
        else:
            source_id = derived_id("src", scope, "request", command.request_id)
        content_hash = hashlib.sha256(draft.content.encode("utf-8")).hexdigest()

        existing = await self.get_source(scope, source_id)
        if existing is not None:
            return self._duplicate(existing, content_hash, command)

        record = self._record(
            Source,
            {
                "id": source_id,
                "source_id": source_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "version": 1,
                "provenance": draft.provenance.value,
                "source_type": draft.source_type,
                "adapter_id": draft.adapter_id,
                "adapter_version": draft.adapter_version,
                "external_id": draft.external_id,
                "occurred_at": draft.occurred_at.isoformat(),
                "registered_at": now.isoformat(),
                "author_id": draft.author_id,
                "identity_confidence": draft.identity_confidence,
                "content": draft.content,
                "retention": {
                    "policy_id": POLICY_ID,
                    "expires_at": expires.isoformat() if expires else None,
                    "protected": draft.protected,
                },
                "availability": "available",
                "read_allowed": draft.read_allowed,
                "api_transfer_allowed": draft.api_transfer_allowed,
                "public_disclosure_allowed": draft.public_disclosure_allowed,
            },
        )
        try:
            async with self.storage.transaction(self.principal, scope, command) as unit:
                if unit.replay is not None:
                    receipt = unit.replay
                    return RegisterResult(
                        receipt.resource_id or source_id, receipt.version or 1, False, True, False
                    )
                await unit.put(record, access=self.reader_access or None)
                await unit.complete(
                    Receipt(
                        request_id=command.request_id,
                        scope=scope,
                        status="completed",
                        resource_id=source_id,
                        version=1,
                    )
                )
        except ContractError as error:
            if error.error.code is not ErrorCode.VERSION_CONFLICT:
                raise
            # Lost a race, or the identity was registered before and later deleted/expired.
            winner = await self.get_source(scope, source_id)
            if winner is None:
                raise ContractError(ErrorCode.ACCESS_DENIED, command.request_id) from None
            return self._duplicate(winner, content_hash, command)
        indexed = await self._index_source(scope, record)
        return RegisterResult(source_id, 1, False, False, indexed)

    def _duplicate(
        self, existing: Source, content_hash: str, command: CommandBase
    ) -> RegisterResult:
        same = (
            existing.content is not None
            and hashlib.sha256(existing.content.encode("utf-8")).hexdigest() == content_hash
        )
        if not same:
            # An edit of an external message is a separate event, not a silent overwrite.
            raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, existing.version)
        return RegisterResult(existing.id, existing.version, True, False, False)

    async def _transition(
        self,
        command: CommandBase,
        source_id: str,
        expected_version: int,
        changes: dict[str, Any],
        *,
        remove_from_index: bool,
    ) -> Receipt:
        scope = command.scope
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            current = await self.get_source(scope, source_id)
            if current is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
            if current.version != expected_version:
                raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, current.version)
            now = max(self.clock(), current.updated_at)
            revised = self._revised(
                current,
                version=current.version + 1,
                updated_at=now.isoformat(),
                **changes,
            )
            await unit.put(
                revised, expected_version=current.version, access=self.reader_access or None
            )
            receipt = Receipt(
                request_id=command.request_id,
                scope=scope,
                status="completed",
                resource_id=source_id,
                version=revised.version,
            )
            await unit.complete(receipt)
        if remove_from_index:
            await self._unindex_source(scope, source_id)
        else:
            await self._index_source(scope, revised)
        return receipt

    async def set_rights(
        self,
        command: CommandBase,
        source_id: str,
        expected_version: int,
        *,
        read_allowed: bool,
        api_transfer_allowed: bool,
        public_disclosure_allowed: bool,
    ) -> Receipt:
        return await self._transition(
            command,
            source_id,
            expected_version,
            {
                "read_allowed": read_allowed,
                "api_transfer_allowed": api_transfer_allowed,
                "public_disclosure_allowed": public_disclosure_allowed,
            },
            remove_from_index=False,
        )

    async def expire_source(
        self, command: CommandBase, source_id: str, expected_version: int
    ) -> Receipt:
        """Retire the raw text before its TTL; the record stays as provenance without content."""
        return await self._transition(
            command,
            source_id,
            expected_version,
            {"availability": "expired", "content": None, "artifact": None},
            remove_from_index=True,
        )

    async def delete_source(
        self, command: CommandBase, source_id: str, expected_version: int
    ) -> Receipt:
        """Logical deletion; the identity cannot be re-imported. Physical purge is M5."""
        return await self._transition(
            command,
            source_id,
            expected_version,
            {"availability": "deleted", "content": None, "artifact": None},
            remove_from_index=True,
        )

    # ---- episodes and claims --------------------------------------------------------------

    async def correct(self, command: EditCommand, operator_id: str) -> Receipt:
        """One owner transaction includes the new revision, audit and durable replay receipt.

        The trusted composition must check the operator's record access and STOPPED fence.
        No source is edited, no certainty raised, and no retention or disclosure right extended.
        """
        args = self.extensions.validate(
            command.arguments.schema_id, command.arguments.schema_version, command.arguments.data
        )
        if (
            command.type != "memory.correct"
            or command.arguments.schema_id != "memory.correct-args"
            or not isinstance(args, MemoryCorrectArguments)
            or args.record_id != command.target
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        model: type[Claim] | type[Summary] = Claim if args.kind == "claim" else Summary
        row = await self.storage.repository(model, self.principal, command.scope).get(
            command.target
        )
        if row is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        # Operator namespacing prevents one operator from claiming another's receipt.
        internal = command.model_copy(
            update={
                "request_id": derived_id(
                    "memory-edit", command.scope, operator_id, command.request_id
                )
            }
        )
        async with self.storage.transaction(self.principal, command.scope, internal) as unit:
            if unit.replay is not None:
                return unit.replay.model_copy(update={"request_id": command.request_id})
            if row.version != command.expected_version:
                raise ContractError(ErrorCode.VERSION_CONFLICT, command.request_id, row.version)
            if args.confidence is not None and (
                not isinstance(row, Claim) or args.confidence >= row.confidence
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            if args.content in (None, row.content) and args.confidence is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            changes: dict[str, Any] = dict(
                version=row.version + 1,
                updated_at=max(self.clock(), row.updated_at).isoformat(),
                scope=command.scope.model_dump(mode="json"),
                correction=MemoryCorrection(
                    operator_id=operator_id,
                    request_id=command.request_id,
                    previous_version=row.version,
                    reason=args.reason.strip(),
                ).model_dump(mode="json"),
            )
            if args.content is not None:
                changes["content"] = args.content
                if isinstance(row, Claim) and args.content != row.content:
                    # An operator rewrite is explicitly an interpretation, not a new observation.
                    changes["knowledge_type"] = "interpretation"
            if args.confidence is not None:
                changes["confidence"] = args.confidence
            updated = self._revised(row, **changes)
            await unit.put(updated, expected_version=row.version)
            receipt = Receipt(
                request_id=internal.request_id,
                scope=command.scope,
                status="completed",
                resource_id=row.id,
                version=updated.version,
            )
            await unit.complete(receipt)
        return receipt.model_copy(update={"request_id": command.request_id})

    async def record_episode(self, command: CommandBase, draft: EpisodeDraft) -> tuple[str, bool]:
        """Returns (episode_id, duplicate). One confirmed operation yields one episode."""
        scope = command.scope
        episode_id = derived_id("ep", scope, draft.operation_id)
        repository = self.storage.repository(Episode, self.principal, scope)
        if await repository.get(episode_id) is not None:
            return episode_id, True
        record = self._record(
            Episode,
            {
                "id": episode_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": self.clock().isoformat(),
                "sources": [ref.model_dump(mode="json") for ref in draft.sources],
                "experience_source_ids": list(draft.experience_source_ids),
                "participant_ids": list(draft.participant_ids),
                "choice": draft.choice,
                "actual_outcome": draft.actual_outcome,
                "interpretation": draft.interpretation,
                "operation_id": draft.operation_id,
                "provenance": draft.provenance,
                "outcome_confirmed": True,
            },
        )
        try:
            async with self.storage.transaction(self.principal, scope, command) as unit:
                if unit.replay is not None:
                    return episode_id, False
                await unit.put(record, access=self.reader_access or None)
                await unit.complete(
                    Receipt(
                        request_id=command.request_id,
                        scope=scope,
                        status="completed",
                        resource_id=episode_id,
                        version=1,
                    )
                )
        except ContractError as error:
            if error.error.code is ErrorCode.VERSION_CONFLICT:
                return episode_id, True
            raise
        if self.index is not None:
            try:
                await self.index.set_group(
                    scope,
                    list(draft.experience_source_ids) or [ref.source_id for ref in draft.sources],
                    "episode:" + episode_id,
                )
            except Exception:
                for ref in draft.sources:
                    self.health.mark(scope, ref.source_id)
        return episode_id, False

    async def record_claim(self, command: CommandBase, draft: ClaimDraft) -> Receipt:
        """A new claim keeps every conflicting position; links are written on both sides."""
        scope = command.scope
        if draft.claim_id in draft.conflicts_with or len(set(draft.conflicts_with)) != len(
            draft.conflicts_with
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
        repository = self.storage.repository(Claim, self.principal, scope)
        now = self.clock()
        touched: list[Claim] = []
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            opposed: list[Claim] = []
            for other_id in draft.conflicts_with:
                other = await repository.get(other_id)
                if other is None:
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS, command.request_id)
                opposed.append(other)
            created = self._record(
                Claim,
                {
                    "id": draft.claim_id,
                    "scope": scope.model_dump(mode="json"),
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "version": 1,
                    "subject_id": draft.subject_id,
                    "content": draft.content,
                    "knowledge_type": draft.knowledge_type,
                    "sources": [ref.model_dump(mode="json") for ref in draft.sources],
                    "confidence": draft.confidence,
                    "conflict_ids": list(draft.conflicts_with),
                },
            )
            await unit.put(created, access=self.reader_access or None)
            touched.append(created)
            for other in opposed:
                if draft.claim_id in other.conflict_ids:
                    continue
                updated = self._revised(
                    other,
                    version=other.version + 1,
                    updated_at=max(now, other.updated_at).isoformat(),
                    conflict_ids=[*other.conflict_ids, draft.claim_id],
                )
                await unit.put(updated, expected_version=other.version)
                touched.append(updated)
            receipt = Receipt(
                request_id=command.request_id,
                scope=scope,
                status="completed",
                resource_id=draft.claim_id,
                version=1,
            )
            await unit.complete(receipt)
        await self._link_claims(scope, touched)
        return receipt

    async def _link_claims(self, scope: Scope, claims: Sequence[Claim]) -> None:
        if self.index is None:
            return
        for claim in claims:
            link = ClaimLink(
                claim.id,
                tuple(dict.fromkeys(ref.source_id for ref in claim.sources)),
                tuple(claim.conflict_ids),
            )
            try:
                await self.index.put_claim(scope, link)
            except Exception:
                for ref in claim.sources:
                    self.health.mark(scope, ref.source_id)

    # ---- derived index maintenance --------------------------------------------------------

    async def _all[R: Any](self, model: type[R], scope: Scope) -> list[R]:
        repository = self.storage.repository(model, self.principal, scope)
        found: list[R] = []
        after: str | None = None
        while True:
            page = await repository.list(limit=LIST_PAGE, after_id=after)
            found.extend(page)
            if len(page) < LIST_PAGE:
                return found
            after = page[-1].id

    async def reindex_scope(self, scope: Scope) -> int:
        """Rebuild one scope atomically from the authority (also after an embedder change)."""
        if self.index is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        sources = await self._all(Source, scope)
        groups: dict[str, str] = {}
        for episode in await self._all(Episode, scope):
            for ref in episode.sources:
                if (
                    episode.experience_source_ids
                    and ref.source_id not in episode.experience_source_ids
                ):
                    continue
                key = "episode:" + episode.id
                groups[ref.source_id] = min(groups.get(ref.source_id, key), key)
        chunk_sets = [chunk_text(s.content or "", self.max_chunk_chars) for s in sources]
        flat = [chunk.text for chunks in chunk_sets for chunk in chunks]
        vectors = await self._embed(flat)
        entries: list[IndexedSource] = []
        position = 0
        for source, chunks in zip(sources, chunk_sets, strict=True):
            indexed = tuple(
                IndexedChunk(
                    chunk, tuple(vectors[position + number]) if vectors is not None else None
                )
                for number, chunk in enumerate(chunks)
            )
            position += len(chunks)
            entries.append(
                IndexedSource(
                    source.id,
                    source.version,
                    source.retention.expires_at,
                    indexed,
                    groups.get(source.id),
                )
            )
        links = [
            ClaimLink(
                claim.id,
                tuple(dict.fromkeys(ref.source_id for ref in claim.sources)),
                tuple(claim.conflict_ids),
            )
            for claim in await self._all(Claim, scope)
        ]
        signature = self.embedder.signature if vectors is not None and self.embedder else None
        await self.index.replace_scope(scope, entries, links, signature)
        self.health.clear(scope)
        return len(entries)

    async def reconcile(self, scope: Scope) -> ReconcileReport:
        """Compare index with authority; rebuild the scope only when they differ."""
        if self.index is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        authority = {s.id: s.version for s in await self._all(Source, scope)}
        indexed = await self.index.inventory(scope)
        missing = tuple(sorted(authority.keys() - indexed.keys()))
        extra = tuple(sorted(indexed.keys() - authority.keys()))
        stale = tuple(
            sorted(k for k in authority.keys() & indexed.keys() if authority[k] != indexed[k])
        )
        dirty = bool(missing or extra or stale or self.health.pending(scope))
        if dirty:
            await self.reindex_scope(scope)
        return ReconcileReport(missing, stale, extra, dirty)

    async def purge_index(self, now: datetime | None = None) -> int:
        """Drop index entries past TTL. Authoritative cleanup of expired payloads is M5-01."""
        if self.index is None:
            return 0
        removed = await self.index.purge_expired(now or self.clock())
        await self.index.compact()
        return removed
