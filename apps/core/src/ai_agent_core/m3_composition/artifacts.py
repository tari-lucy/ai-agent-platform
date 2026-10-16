"""Bounded full laboratory reports. Catalogue/source rights stay authoritative."""

import hashlib
import json
import os
import stat
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path

from ai_agent_adapters.local_files import restrict_private
from ai_agent_application.laboratory.report import ScenarioReport
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner, Principal, ReadPurpose, RecordAccess, Storage
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Retention, Source
from ai_agent_contracts.states import ErrorCode, Provenance
from ai_agent_contracts.workspace import Artifact
from pydantic import TypeAdapter, ValidationError

from .state import PRINCIPALS, put


class Reports:
    def __init__(
        self,
        root: Path,
        storage: Storage,
        registry: ExtensionRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(days=30),
        readers: tuple[RecordAccess, ...] = (),
        api_transfer_allowed: bool = False,
        max_bytes: int = 2_000_000,
        quota_bytes: int = 32_000_000,
    ):
        if (
            not timedelta(0) < ttl <= timedelta(days=30)
            or not 1 <= max_bytes <= quota_bytes <= 128_000_000
        ):
            raise ValueError("REPORT_LIMIT")
        self.root, self.storage, self.registry = root.absolute(), storage, registry
        self._safe_root()
        self.clock, self.ttl, self.readers = clock, ttl, readers
        self.api_transfer_allowed = api_transfer_allowed
        self.max_bytes, self.quota_bytes = max_bytes, quota_bytes

    def _safe_root(self) -> None:
        if any(p.is_symlink() or p.is_junction() for p in (self.root, *self.root.parents)):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def _body(self, scope: Scope, source_id: str, reference: ArtifactRef) -> bytes:
        self._safe_root()
        path = self._path(scope, source_id)
        if path.is_symlink() or path.is_junction():
            raise ContractError(ErrorCode.ACCESS_DENIED)
        try:
            if path.stat().st_size > min(self.max_bytes, reference.size_bytes):
                raise ContractError(ErrorCode.STALE_CONTEXT)
            with path.open("rb") as handle:
                body = handle.read(self.max_bytes + 1)
        except OSError:
            raise ContractError(ErrorCode.STALE_CONTEXT) from None
        if (
            len(body) != reference.size_bytes
            or hashlib.sha256(body).hexdigest() != reference.checksum
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return body

    @staticmethod
    def _references(scope: Scope, body: bytes) -> tuple[SourceRef, ...]:
        try:
            value = json.loads(body)
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("source_refs"), list)
                or len(value["source_refs"]) > 2000
            ):
                raise ValueError
            refs = TypeAdapter(tuple[SourceRef, ...]).validate_python(value["source_refs"])
            if value.get("source_ids") != sorted({r.source_id for r in refs}) or any(
                r.scope.partition != scope.partition for r in refs
            ):
                raise ValueError
        except ValueError, TypeError, ValidationError:
            raise ContractError(ErrorCode.STALE_CONTEXT) from None
        return refs

    async def _check_sources(
        self,
        scope: Scope,
        refs: Sequence[SourceRef],
        principal: Principal,
        purpose: ReadPurpose = "private",
    ) -> None:
        repo = self.storage.repository(Source, principal, scope)
        for ref in refs:
            if ref.scope.partition != scope.partition:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            source = await repo.get(ref.source_id, purpose=purpose)
            if (
                source is None
                or source.version != ref.version
                or source.availability != "available"
                or source.retention.expires_at is not None
                and source.retention.expires_at <= self.clock()
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)

    def _path(self, scope: Scope, source_id: str) -> Path:
        key = hashlib.sha256(("\0".join(scope.partition) + "\0" + source_id).encode()).hexdigest()
        return self.root / (key + ".json")

    async def save(self, scope: Scope, report: ScenarioReport) -> ArtifactRef:
        if scope.memory_mode != "test" or report.mock is not True:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if report.scope is not None and report.scope.partition != scope.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        report.scope = scope
        if report.source_refs is None:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        await self._check_sources(scope, report.source_refs, PRINCIPALS[Owner.MEMORY])
        body = report.to_json().encode("utf-8")
        if len(body) > self.max_bytes:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        sid = derived_id("labsource", scope, report.digest)
        aid = derived_id("labartifact", scope, report.digest)
        checksum = hashlib.sha256(body).hexdigest()
        reference = ArtifactRef(
            artifact_id=aid,
            version=1,
            scope=scope,
            checksum=checksum,
            size_bytes=len(body),
            media_type="application/json",
        )
        prior = await self.storage.repository(Artifact, PRINCIPALS[Owner.CATALOGUE], scope).get(aid)
        if prior is not None:
            if prior.reference != reference:
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            prior_body = self._body(scope, sid, reference)
            await self._check_sources(
                scope, self._references(scope, prior_body), PRINCIPALS[Owner.MEMORY]
            )
            return reference
        # Never recreate a hidden/tombstoned report under its old identity.
        now = self.clock()
        source = Source(
            id=sid,
            source_id=sid,
            scope=scope,
            version=1,
            created_at=now,
            updated_at=now,
            provenance=Provenance.SIMULATION,
            source_type="laboratory-report",
            adapter_id="m3-laboratory",
            adapter_version="1",
            occurred_at=now,
            registered_at=now,
            content="MOCK report " + report.digest,
            artifact=reference,
            retention=Retention(policy_id="laboratory-30d", expires_at=now + self.ttl),
            availability="available",
            read_allowed=True,
            api_transfer_allowed=self.api_transfer_allowed,
            public_disclosure_allowed=False,
        )
        prior_source = await self.storage.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
            sid
        )
        if prior_source is not None:
            if prior_source.artifact != reference or prior_source.content != source.content:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            now = prior_source.created_at
            source = prior_source
        else:
            await put(
                self.storage,
                self.registry,
                Owner.MEMORY,
                scope,
                "report-source-" + sid,
                source,
                None,
                self.readers,
            )
        deadline = source.retention.expires_at
        if deadline is None or deadline <= self.clock():
            raise ContractError(ErrorCode.EXPIRED)
        self._safe_root()
        self.root.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
        restrict_private(self.root, directory=True)
        path = self._path(scope, sid)
        self.storage.check_fence()
        self.sweep()
        expiry = path.with_suffix(".ttl")
        if not expiry.exists():
            expiry.touch(mode=0o600)
            restrict_private(expiry, directory=False)
            expiry.write_text(deadline.isoformat(), encoding="ascii")
        if not path.exists():
            files = list(islice(self.root.glob("*.json"), 1001))
            if len(files) > 1000:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            used = sum(p.stat().st_size for p in files)
            if used + len(body) > self.quota_bytes:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            pending = path.with_suffix(".pending")
            if pending.exists() or pending.is_symlink() or pending.is_junction():
                raise ContractError(ErrorCode.STALE_CONTEXT)
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    restrict_private(pending, directory=False)
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                await self._check_sources(scope, report.source_refs, PRINCIPALS[Owner.MEMORY])
                self.storage.check_fence()
                self._safe_root()
                os.replace(pending, path)
            finally:
                if not pending.is_symlink() and not pending.is_junction():
                    pending.unlink(missing_ok=True)
        elif (
            path.is_symlink()
            or path.is_junction()
            or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        artifact = Artifact(
            id=aid,
            scope=scope,
            version=1,
            created_at=now,
            updated_at=now,
            reference=reference,
            provenance_source=SourceRef(
                source_id=sid, version=1, scope=scope, availability="available"
            ),
            expires_at=source.retention.expires_at,
            protected=False,
        )
        await put(
            self.storage,
            self.registry,
            Owner.CATALOGUE,
            scope,
            "report-artifact-" + aid,
            artifact,
            None,
            self.readers,
        )
        return reference

    async def read(
        self,
        scope: Scope,
        artifact_id: str,
        principal: Principal,
        *,
        purpose: ReadPurpose = "private",
        expected: ArtifactRef | None = None,
    ) -> bytes:
        artifact = await self.storage.repository(Artifact, principal, scope).get(
            artifact_id, purpose=purpose
        )
        if artifact is None or artifact.expires_at is None or artifact.expires_at <= self.clock():
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if expected is not None and artifact.reference != expected:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        body = self._body(scope, artifact.provenance_source.source_id, artifact.reference)
        refs = self._references(scope, body)
        # The preliminary verified read discovers lineage; guard the actual read on both sides.
        await self._check_sources(scope, refs, principal, purpose)
        if self._body(scope, artifact.provenance_source.source_id, artifact.reference) != body:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        # A revoke/delete during file IO must also close the response.
        again = await self.storage.repository(Artifact, principal, scope).get(
            artifact_id, purpose=purpose
        )
        if (
            again is None
            or again != artifact
            or again.expires_at is None
            or again.expires_at <= self.clock()
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await self._check_sources(scope, refs, principal, purpose)
        return body

    def holds(self, scope: Scope, source_id: str) -> bool:
        """A pending write also holds data and must not be called purged."""
        self._safe_root()
        path = self._path(scope, source_id)
        present = False
        for item in (path, path.with_suffix(".pending")):
            try:
                metadata = item.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ContractError(ErrorCode.RESOURCE_BUSY) from None
            if not stat.S_ISREG(metadata.st_mode) or item.is_junction():
                raise ContractError(ErrorCode.ACCESS_DENIED)
            present = True
        return present

    async def purge(self, scope: Scope, source_id: str) -> None:
        self._safe_root()
        path = self._path(scope, source_id)
        files = (path, path.with_suffix(".ttl"), path.with_suffix(".pending"))
        if any(p.is_symlink() or p.is_junction() for p in files):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        for item in files:
            item.unlink(missing_ok=True)
        if self.holds(scope, source_id):
            raise ContractError(ErrorCode.RESOURCE_BUSY)

    async def _inventory(self, scope: Scope) -> dict[str, ArtifactRef | None]:
        # A report Source is saved before its file, including unfinished catalogue saves.
        # No dependency is reconstructed from body text or an untrusted filename.
        found: dict[str, ArtifactRef | None] = {}
        for model, owner in ((Source, Owner.MEMORY), (Artifact, Owner.CATALOGUE)):
            after = None
            for _ in range(50):
                rows = await self.storage.repository(model, PRINCIPALS[owner], scope).list(
                    limit=200, after_id=after
                )
                for row in rows:
                    if isinstance(row, Source) and row.source_type == "laboratory-report":
                        found.setdefault(row.id, row.artifact)
                    elif isinstance(row, Artifact):
                        found[row.provenance_source.source_id] = row.reference
                if len(rows) < 200:
                    break
                after = rows[-1].id
            else:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
        return found

    async def purge_for_source(self, scope: Scope, source_id: str) -> None:
        await self.purge(scope, source_id)
        self.sweep()
        unproven = False
        known_paths: set[Path] = set()
        for report_id, reference in (await self._inventory(scope)).items():
            known_paths.add(self._path(scope, report_id))
            known_paths.add(self._path(scope, report_id).with_suffix(".pending"))
            if not self.holds(scope, report_id):
                continue
            try:
                if (
                    reference is None
                    or self._path(scope, report_id).with_suffix(".pending").exists()
                ):
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                refs = self._references(scope, self._body(scope, report_id, reference))
            except ContractError:
                unproven = True
                continue
            if any(ref.source_id == source_id for ref in refs):
                await self.purge(scope, report_id)
        # An unregistered body must never let retention claim physical cleanup. Prove
        # foreign files against their owner reference; an unproven legacy/orphan stays blocked.
        remaining = list(islice(self.root.iterdir(), 3001))
        if len(remaining) > 3000:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        for path in remaining:
            if path.suffix not in (".json", ".pending") or path in known_paths:
                continue
            try:
                self._safe_root()
                if path.is_symlink() or path.is_junction() or path.stat().st_size > self.max_bytes:
                    raise ValueError
                value = json.loads(path.read_bytes())
                foreign = Scope.model_validate(value["scope"])
                sid = derived_id("labsource", foreign, value["digest"])
                source = await self.storage.repository(
                    Source, PRINCIPALS[Owner.MEMORY], foreign
                ).get(sid)
                if (
                    foreign.partition == scope.partition
                    or path != self._path(foreign, sid)
                    or source is None
                    or source.artifact is None
                ):
                    raise ValueError
                self._references(foreign, self._body(foreign, sid, source.artifact))
            except ValueError, KeyError, TypeError, OSError, ContractError:
                unproven = True
        if unproven:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    async def purge_sources(self, scope: Scope, source_ids: Sequence[str]) -> None:
        for source_id in source_ids:
            await self.purge_for_source(scope, source_id)

    def sweep(self) -> int:
        """Bounded TTL cleanup also covers files left by a failed metadata transaction."""
        self._safe_root()
        removed = 0
        files = list(islice(self.root.glob("*.ttl"), 1001))
        if len(files) > 1000:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        for expiry in files:
            if expiry.is_symlink() or expiry.stat().st_size > 128:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            deadline = datetime.fromisoformat(expiry.read_text(encoding="ascii"))
            if deadline <= self.clock():
                path = expiry.with_suffix(".json")
                pending = path.with_suffix(".pending")
                if path.is_symlink() or pending.is_symlink() or pending.is_junction():
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                path.unlink(missing_ok=True)
                pending.unlink(missing_ok=True)
                expiry.unlink()
                removed += 1
        return removed
