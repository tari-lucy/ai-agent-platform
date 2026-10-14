"""Bounded full laboratory reports. Catalogue/source rights stay authoritative."""

import hashlib
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path

from ai_agent_application.laboratory.report import ScenarioReport
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner, Principal, RecordAccess, Storage
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.memory import Retention, Source
from ai_agent_contracts.states import ErrorCode, Provenance
from ai_agent_contracts.workspace import Artifact

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
        max_bytes: int = 2_000_000,
        quota_bytes: int = 32_000_000,
    ):
        if (
            not timedelta(0) < ttl <= timedelta(days=30)
            or not 1 <= max_bytes <= quota_bytes <= 128_000_000
        ):
            raise ValueError("REPORT_LIMIT")
        self.root, self.storage, self.registry = root.resolve(), storage, registry
        self.clock, self.ttl, self.readers = clock, ttl, readers
        self.max_bytes, self.quota_bytes = max_bytes, quota_bytes

    def _path(self, scope: Scope, source_id: str) -> Path:
        key = hashlib.sha256(("\0".join(scope.partition) + "\0" + source_id).encode()).hexdigest()
        return self.root / (key + ".json")

    async def save(self, scope: Scope, report: ScenarioReport) -> ArtifactRef:
        if scope.memory_mode != "test" or report.mock is not True:
            raise ContractError(ErrorCode.ACCESS_DENIED)
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
            await self.read(scope, aid, PRINCIPALS[Owner.CATALOGUE])
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
            api_transfer_allowed=False,
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
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(scope, sid)
        self.storage.check_fence()
        self.sweep()
        expiry = path.with_suffix(".ttl")
        if not expiry.exists():
            expiry.write_text(deadline.isoformat(), encoding="ascii")
        if not path.exists():
            files = list(islice(self.root.glob("*.json"), 1001))
            if len(files) > 1000:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            used = sum(p.stat().st_size for p in files)
            if used + len(body) > self.quota_bytes:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        elif path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
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

    async def read(self, scope: Scope, artifact_id: str, principal: Principal) -> bytes:
        artifact = await self.storage.repository(Artifact, principal, scope).get(artifact_id)
        if artifact is None or artifact.expires_at is None or artifact.expires_at <= self.clock():
            raise ContractError(ErrorCode.ACCESS_DENIED)
        path = self._path(scope, artifact.provenance_source.source_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_bytes:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        body = path.read_bytes()
        if (
            len(body) != artifact.reference.size_bytes
            or hashlib.sha256(body).hexdigest() != artifact.reference.checksum
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        # A revoke/delete during file IO must also close the response.
        again = await self.storage.repository(Artifact, principal, scope).get(artifact_id)
        if again is None or again != artifact:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return body

    async def purge(self, scope: Scope, source_id: str) -> None:
        # Trusted M5 DerivedPurger port: deterministic path, no arbitrary client pathname.
        path = self._path(scope, source_id)
        if path.is_symlink():
            raise ContractError(ErrorCode.ACCESS_DENIED)
        path.unlink(missing_ok=True)
        path.with_suffix(".ttl").unlink(missing_ok=True)

    async def purge_sources(self, scope: Scope, source_ids: Sequence[str]) -> None:
        for source_id in source_ids:
            await self.purge(scope, source_id)

    def sweep(self) -> int:
        """Bounded TTL cleanup also covers files left by a failed metadata transaction."""
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
                if path.is_symlink():
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                path.unlink(missing_ok=True)
                expiry.unlink()
                removed += 1
        return removed
