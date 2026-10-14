"""GM settings adapter and durable progress for transfers across distinct owners."""

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from ai_agent_application.laboratory.transfer import (
    NEVER_TRANSFERRED,
    Key,
    SettingsTransfer,
    TransferPreview,
    TransferResult,
)
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner
from ai_agent_contracts.codec import ContractError, to_wire
from ai_agent_contracts.common import Scope
from ai_agent_contracts.persona import GmRuntimeState, SettingsTransferOperation
from ai_agent_contracts.states import ErrorCode

from .state import PRINCIPALS, State, put


class DurableSettings:
    def __init__(self, state: State):
        self.state = state
        self.versions: dict[tuple[tuple[str, str, str, str], str], int] = {}
        self.snapshots: dict[tuple[tuple[str, str, str, str], str], GmRuntimeState] = {}
        self.sources: dict[str, GmRuntimeState] = {}

    async def get(self, scope: Scope, key: str) -> Any:
        value = await self.state.gm(scope)
        self.versions[(scope.partition, key)] = value.version
        self.snapshots[(scope.partition, key)] = value
        if scope.memory_mode == "test":
            self.sources[key] = value
        if key == "gm_intensity":
            return value.intensity
        if key == "story_rules_version":
            return value.rules_version
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    def fingerprint(self, scope: Scope, key: str) -> str:
        return hashlib.sha256(
            to_wire(self.snapshots[(scope.partition, key)], extensions=self.state.registry)
        ).hexdigest()

    async def set(self, scope: Scope, key: str, value: Any) -> None:
        old = await self.state.gm(scope)
        expected = self.versions.get((scope.partition, key))
        if old.version != expected:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        source = self.sources.get(key)
        if source is None or (await self.state.gm(source.scope)).version != source.version:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        changes: dict[str, Any]
        if key == "gm_intensity" and type(value) in (float, int) and 0 <= value <= 1:
            changes = {"intensity": value}
        elif key == "story_rules_version" and value == source.rules_version:
            if source.rules_version < old.rules_version or (
                source.rules_version == old.rules_version and source.rules != old.rules
            ):
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            from .schemas import RulesData

            before = self.state.registry.validate(
                old.rules.schema_id, old.rules.schema_version, old.rules.data
            )
            after = self.state.registry.validate(
                source.rules.schema_id, source.rules.schema_version, source.rules.data
            )
            assert isinstance(before, RulesData) and isinstance(after, RulesData)
            if any(rule not in after.rules for rule in before.rules if rule.level == "project"):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            changes = {"rules_version": source.rules_version, "rules": source.rules}
        else:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        updated = old.model_copy(
            update={
                **changes,
                "scope": scope,
                "version": old.version + 1,
                "updated_at": max(datetime.now(UTC), old.updated_at),
            }
        )
        await put(
            self.state.storage,
            self.state.registry,
            Owner.GM,
            scope,
            f"gm-setting-{updated.version}",
            updated,
            old.version,
        )
        for cached in list(self.versions):
            if cached[0] == scope.partition:
                self.versions[cached] = updated.version


class TransferJournal:
    def __init__(self, state: State, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.state, self.clock = state, clock

    async def get(self, target: Scope, operation: str) -> SettingsTransferOperation | None:
        return await self.state.storage.repository(
            SettingsTransferOperation, PRINCIPALS[Owner.WORKSPACE], target
        ).get(derived_id("transfer", target, operation))

    async def apply(
        self,
        transfer: SettingsTransfer,
        source: Scope,
        target: Scope,
        keys: Sequence[str],
        operation: str,
        digest: str,
        operator: str,
    ) -> TransferResult:
        old = await self.get(target, operation)
        if old is not None:
            if (
                old.preview_digest != digest
                or old.source_scope.partition != source.partition
                or old.selected_keys != list(keys)
                or old.operator_id != operator
            ):
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            if old.status == "completed":
                return TransferResult(
                    tuple(old.applied_keys),
                    TransferPreview((), NEVER_TRANSFERRED, old.preview_digest),
                )
            # An interrupted cross-owner operation is never blindly replayed.
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        preview = await transfer.preview(source, target, keys)
        if preview.digest != digest or preview.blocked:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        now = self.clock()
        record = SettingsTransferOperation(
            id=derived_id("transfer", target, operation),
            scope=target,
            version=1,
            created_at=now,
            updated_at=now,
            source_scope=source,
            preview_digest=digest,
            selected_keys=cast(list[Key], list(keys)),
            status="planned",
            operator_id=operator,
        )
        await self._save(record, None)
        applied: list[Key] = []
        try:
            # One reviewed item at a time: owner CAS protects profile and settings.
            for item in sorted(preview.items, key=lambda item: item.key != "profile"):
                if item.key == "profile":
                    await transfer._apply_profile(source, target, operation, item)
                else:
                    from ai_agent_application.laboratory.transfer import resolved

                    await resolved(transfer.settings.set(target, item.key, item.after))
                applied.append(cast(Key, item.key))
                record = await self._progress(record, applied, "planned")
            record = await self._progress(record, applied, "completed")
        except BaseException:
            # If this write is unavailable, the durable 'planned' record also blocks restart.
            await self._progress(record, applied, "reconciliation_required")
            raise
        return TransferResult(tuple(applied), preview)

    async def acknowledge(self, target: Scope, operation: str, operator: str) -> None:
        old = await self.get(target, operation)
        if old is None or old.operator_id != operator:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if old.status in ("planned", "reconciliation_required"):
            await self._progress(old, old.applied_keys, "acknowledged")

    async def pending(self, target: Scope) -> bool:
        repo = self.state.storage.repository(
            SettingsTransferOperation, PRINCIPALS[Owner.WORKSPACE], target
        )
        after = None
        for _ in range(50):
            page = await repo.list(limit=200, after_id=after)
            if any(r.status in ("planned", "reconciliation_required") for r in page):
                return True
            if len(page) < 200:
                return False
            after = page[-1].id
        raise ContractError(ErrorCode.RESOURCE_BUSY)

    async def _progress(
        self, old: SettingsTransferOperation, applied: Sequence[Key], status: str
    ) -> SettingsTransferOperation:
        record = SettingsTransferOperation.model_validate(
            {
                **old.model_dump(),
                "version": old.version + 1,
                "updated_at": max(self.clock(), old.updated_at),
                "applied_keys": list(applied),
                "status": status,
            }
        )
        await self._save(record, old.version)
        return record

    async def _save(self, record: SettingsTransferOperation, expected: int | None) -> None:
        await put(
            self.state.storage,
            self.state.registry,
            Owner.WORKSPACE,
            record.scope,
            f"transfer-{record.id}-{record.version}",
            record,
            expected,
        )
