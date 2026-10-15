"""Backup / restore / migration guard (M5-02).

Rules (doc23 runbook): copies are consistent snapshots, never a plain file copy of a live WAL
database; 7 daily copies plus a separate copy before migration; a copy never outlives the text
term; a restore is previewed in a staging place, checked, cleaned by the *independent* deletion
ledger, reindexed, verified, and only then switched in - with the agent stopped, and never
started by the restore. A copy whose ledger is missing or older than the copy's is not admitted.
"""

import hashlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .model import (
    DATABASE,
    FORMAT,
    BackupResult,
    FileEntry,
    Issue,
    Kind,
    Manifest,
    MigrationReport,
    RestorePreview,
    RestoreReport,
)
from .policy import BackupPolicy
from .ports import (
    AgentGate,
    BackupStore,
    ExtraFiles,
    LedgerView,
    Snapshotter,
    StagingFactory,
    Switcher,
)

STOPPED = "STOPPED"
CHUNK = 1 << 20
_UNREADABLE = frozenset({"CHECKSUM_MISMATCH", "FILE_MISSING_OR_RESIZED", "INTEGRITY_FAILED"})

# Composition-supplied compatibility check of a stored copy with the current host
# (manifest, directory of the copy). Runs inside the preview, so its issues are part of the digest.
RestoreCheck = Callable[[Manifest, Path], Awaitable[list[Issue]]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class BackupService:
    def __init__(
        self,
        snapshotter: Snapshotter,
        store: BackupStore,
        ledger: LedgerView,
        gate: AgentGate,
        staging: StagingFactory,
        switcher: Switcher,
        *,
        work_dir: Path,
        current_head: str,
        close_live: Callable[[], Awaitable[None]],
        ledger_dir: Path | None = None,
        extra: ExtraFiles | None = None,
        upgrade_staged: Callable[[Path], Awaitable[None]] | None = None,
        restore_check: RestoreCheck | None = None,
        policy: BackupPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        software: str = "",
    ) -> None:
        self.snapshotter, self.store, self.ledger, self.gate = snapshotter, store, ledger, gate
        self.staging, self.switcher = staging, switcher
        self.work_dir, self.current_head, self.close_live = work_dir, current_head, close_live
        self.ledger_dir, self.extra, self.upgrade_staged = ledger_dir, extra, upgrade_staged
        self.restore_check = restore_check
        self.policy, self.clock, self.software = policy or BackupPolicy(), clock, software

    # ---- creating copies ------------------------------------------------------------------

    async def create(
        self, kind: Kind = "daily", *, keep: frozenset[str] = frozenset()
    ) -> BackupResult:
        now = self.clock()
        quota = self.policy.quota_bytes
        if quota is not None and self.store.used_bytes() >= quota:
            raise ContractError(ErrorCode.RESOURCE_BUSY)  # full: no new copy, nothing is erased
        existing = {m.backup_id for m in await self.store.manifests()}
        stem = f"bk-{now.strftime('%Y%m%dT%H%M%S')}-{kind}"
        backup_id, number = stem, 1
        while backup_id in existing:
            number += 1
            backup_id = f"{stem}-{number}"
        stage = self.work_dir / f"make-{backup_id}"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700)
        try:
            database = stage / DATABASE
            await self.snapshotter.snapshot(database)
            problems = await self.snapshotter.integrity(database)
            if problems:
                raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)  # never keep a broken copy
            head = await self.snapshotter.head(database)
            ledger_id, ledger_seq = await self.ledger.identity()
            files: list[tuple[str, Path]] = [(DATABASE, database)]
            for name, source in self.extra.files() if self.extra else []:
                if (
                    "/" in name
                    or "\\" in name
                    or name in {"", ".", "..", DATABASE, "manifest.json"}
                ):
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                files.append((name, source))
            entries = tuple(
                FileEntry(name, sha256_file(path), path.stat().st_size) for name, path in files
            )
            manifest = Manifest(
                FORMAT, backup_id, kind, now, head, ledger_id, ledger_seq, entries, self.software
            )
            await self.store.write(backup_id, kind, files, manifest)
            if self.ledger_dir is not None:
                self.ledger_dir.mkdir(
                    parents=True, exist_ok=True, mode=0o777 if os.name == "nt" else 0o700
                )
                await self.ledger.snapshot_to(self.ledger_dir / f"{backup_id}.ledger.sqlite")
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        pruned = await self._prune(keep)
        return BackupResult(manifest, tuple(pruned), tuple(self._quota_warnings()))

    def _quota_warnings(self) -> list[str]:
        quota = self.policy.quota_bytes
        if quota is None:
            return []
        used = self.store.used_bytes() / quota
        return [f"QUOTA_{level}" for level in (90, 80) if used >= level / 100][:1]

    async def _prune(self, keep_ids: frozenset[str] = frozenset()) -> list[str]:
        now = self.clock()
        manifests = sorted(await self.store.manifests(), key=lambda m: m.created_at, reverse=True)
        keep = {
            "daily": self.policy.keep_daily,
            "pre_migration": self.policy.keep_pre_migration,
            "pre_restore": self.policy.keep_pre_migration,
        }
        seen: dict[str, int] = {}
        removed: list[str] = []
        for manifest in manifests:
            seen[manifest.kind] = seen.get(manifest.kind, 0) + 1
            if manifest.backup_id in keep_ids:
                continue  # the copy being restored must survive its own restore
            too_many = manifest.kind in keep and seen[manifest.kind] > keep[manifest.kind]
            if too_many or now - manifest.created_at > self.policy.max_age:
                await self.store.remove(manifest.backup_id)
                if self.ledger_dir is not None:
                    (self.ledger_dir / f"{manifest.backup_id}.ledger.sqlite").unlink(
                        missing_ok=True
                    )
                removed.append(manifest.backup_id)
        return removed

    async def copies(self) -> list[Manifest]:
        return sorted(await self.store.manifests(), key=lambda m: m.created_at, reverse=True)

    async def _manifest(self, backup_id: str) -> Manifest:
        for manifest in await self.store.manifests():
            if manifest.backup_id == backup_id:
                return manifest
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    # ---- checking copies ------------------------------------------------------------------

    async def verify(self, backup_id: str) -> list[Issue]:
        manifest = await self._manifest(backup_id)
        issues: list[Issue] = []
        if manifest.format != FORMAT:
            issues.append(Issue("FORMAT_UNKNOWN", manifest.format))
        for entry in manifest.files:
            path = await self.store.path(backup_id, entry.name)
            if not path.is_file() or path.stat().st_size != entry.size:
                issues.append(Issue("FILE_MISSING_OR_RESIZED", entry.name))
            elif sha256_file(path) != entry.sha256:
                issues.append(Issue("CHECKSUM_MISMATCH", entry.name))
        if issues:
            return issues
        database = await self.store.path(backup_id, DATABASE)
        if await self.snapshotter.integrity(database):
            issues.append(Issue("INTEGRITY_FAILED"))
        return issues

    # ---- restore --------------------------------------------------------------------------

    async def preview_restore(self, backup_id: str) -> RestorePreview:
        manifest = await self._manifest(backup_id)
        issues = await self.verify(backup_id)
        head = manifest.schema_head
        needs_migration = False
        if head > self.current_head:
            issues.append(Issue("SCHEMA_NEWER_THAN_SOFTWARE", head))
        elif head < self.current_head:
            needs_migration = True
            if self.upgrade_staged is None:
                issues.append(Issue("MIGRATION_REQUIRED", f"{head} -> {self.current_head}"))
        issues.extend(await self._ledger_issues(manifest))
        if self.restore_check is not None and not any(i.code in _UNREADABLE for i in issues):
            copy_dir = (await self.store.path(backup_id, DATABASE)).parent
            issues.extend(await self.restore_check(manifest, copy_dir))
        state = await self.gate.run_state()
        if state != STOPPED:
            issues.append(Issue("AGENT_NOT_STOPPED", state))
        scopes = at_risk = outbox = 0
        if not any(i.code in _UNREADABLE for i in issues):
            scopes, at_risk, outbox = await self._inspect(backup_id, needs_migration)
        if at_risk:
            issues.append(Issue("OPERATIONS_AT_RISK", str(at_risk), blocking=False))
        digest = self._digest(manifest, issues, scopes, at_risk, outbox)
        return RestorePreview(
            backup_id,
            manifest.kind,
            manifest.created_at,
            head,
            needs_migration,
            tuple(issues),
            scopes,
            at_risk,
            outbox,
            digest,
        )

    async def _ledger_issues(self, manifest: Manifest) -> list[Issue]:
        try:
            ledger_id, seq = await self.ledger.identity()
        except Exception:
            return [Issue("LEDGER_UNAVAILABLE")]
        if ledger_id != manifest.ledger_id:
            return [Issue("LEDGER_UNAVAILABLE", "another or new ledger")]
        if seq < manifest.ledger_seq:
            return [Issue("LEDGER_OLDER_THAN_COPY", f"{seq} < {manifest.ledger_seq}")]
        return []

    async def _inspect(self, backup_id: str, migrate: bool) -> tuple[int, int, int]:
        scratch = self.work_dir / f"inspect-{backup_id}"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700)
        try:
            copy = scratch / DATABASE
            shutil.copyfile(await self.store.path(backup_id, DATABASE), copy)
            if migrate and self.upgrade_staged is not None:
                await self.upgrade_staged(copy)
            target = await self.staging.open(copy)
            try:
                return (
                    len(await target.scopes()),
                    await target.operations_at_risk(),
                    await target.pending_outbox(),
                )
            finally:
                await target.close()
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _digest(self, manifest: Manifest, issues: list[Issue], *counts: int) -> str:
        body = json.dumps(
            [
                manifest.backup_id,
                [(f.name, f.sha256) for f in manifest.files],
                sorted((i.code, i.detail) for i in issues),
                counts,
            ],
            sort_keys=True,
        )
        return hashlib.sha256(body.encode()).hexdigest()

    async def restore(self, preview: RestorePreview, operator_decision_id: str) -> RestoreReport:
        """Operator-confirmed. The agent is not started; the live store is replaced last."""
        if not operator_decision_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if await self.gate.run_state() != STOPPED:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        current = await self.preview_restore(preview.backup_id)
        if current != preview:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        if current.blocked:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        safety = await self.create(
            "pre_restore", keep=frozenset({preview.backup_id})
        )  # a way back, taken before anything changes
        stage = self.work_dir / f"restore-{preview.backup_id}"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700)
        try:
            report = await self._stage(preview, stage, safety.manifest.backup_id)
            await self.close_live()
            await self.switcher.switch(stage)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return report

    async def _stage(self, preview: RestorePreview, stage: Path, safety_id: str) -> RestoreReport:
        database = stage / DATABASE
        shutil.copyfile(await self.store.path(preview.backup_id, DATABASE), database)
        manifest = await self._manifest(preview.backup_id)
        wanted = next(f.sha256 for f in manifest.files if f.name == DATABASE)
        if sha256_file(database) != wanted:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        if preview.needs_migration and self.upgrade_staged is not None:
            await self.upgrade_staged(database)
        target = await self.staging.open(database)
        reapplied: list[str] = []
        expired = reindexed = 0
        verified: list[str] = []
        try:
            for scope in await target.scopes():
                first = await target.apply_ledger(scope)
                reapplied.extend(first.reapplied)
                swept = await target.sweep(scope)
                expired += len(swept.expired)
                again = await target.apply_ledger(scope)
                leftovers = await target.blocked_text_remaining(scope)
                if again.reapplied or again.pending or first.pending or swept.pending or leftovers:
                    raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)  # live store stays as is
                reindexed += await target.rebuild_index(scope)
                verified.append(scope.memory_space_id)
            quarantined = await target.quarantine_outbox()
        finally:
            await target.close()
        if await self.snapshotter.integrity(database):
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE)
        return RestoreReport(
            preview.backup_id,
            safety_id,
            tuple(reapplied),
            expired,
            quarantined,
            reindexed,
            tuple(verified),
        )

    # ---- migration guard ------------------------------------------------------------------

    async def guarded_migration(self, upgrade: Callable[[], Awaitable[None]]) -> MigrationReport:
        """A verified copy first; the upgrade runs only if the copy is good."""
        if await self.gate.run_state() != STOPPED:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        made = await self.create("pre_migration")
        backup_id = made.manifest.backup_id
        problems = await self.verify(backup_id)
        if problems:
            return MigrationReport(backup_id, False, "BACKUP_UNVERIFIED", None, made.warnings)
        try:
            await upgrade()
        except Exception as error:  # the type only: messages may echo data
            return MigrationReport(backup_id, False, type(error).__name__, backup_id, made.warnings)
        return MigrationReport(backup_id, True, "", None, made.warnings)

    async def rehearse_migration(
        self, upgrade_copy: Callable[[Path], Awaitable[None]]
    ) -> MigrationReport:
        """Run the upgrade on a snapshot copy; the live database is not touched."""
        scratch = self.work_dir / "rehearsal"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, mode=0o777 if os.name == "nt" else 0o700)
        try:
            copy = scratch / DATABASE
            await self.snapshotter.snapshot(copy)
            try:
                await upgrade_copy(copy)
            except Exception as error:
                return MigrationReport(None, False, type(error).__name__)
            if await self.snapshotter.integrity(copy):
                return MigrationReport(None, False, "INTEGRITY_FAILED")
            return MigrationReport(None, True, "")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


__all__ = ["BackupService", "sha256_file"]
