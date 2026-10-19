"""Online host-config owner, sharing the ledger RMW lock; no second cap or usage counter."""

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import derived_id
from ai_agent_contracts.api import EditCommand
from ai_agent_contracts.codec import ContractError, command_fingerprint
from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import ApiLimitChange, ApiLimits
from ai_agent_contracts.states import ErrorCode, RunState

from .layout import HostConfig, HostPaths, load_config, save_config

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.service import Runtime


class LiveBudget:
    def __init__(self, host: HostPaths, runtime: Runtime, lock: asyncio.Lock):
        self.host, self.rt, self.lock = host, runtime, lock

    def config(self, scope: Scope) -> HostConfig:
        config = load_config(self.host)
        if (
            not config.operator_controls
            or config.live is None
            or scope.memory_mode != "work"
            or scope.partition
            != (config.workspace_id, config.persona_id, config.work_space_id, "work")
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return config

    async def read(self, scope: Scope) -> ApiLimits:
        async with self.lock:
            live = self.config(scope).live
            assert live is not None
            minimum = (live.memory_daily_calls if live.memory_processing else 0) + 1
            if live.gm.enabled:
                minimum = max(
                    minimum,
                    (live.memory_daily_calls if live.memory_processing else 0)
                    + live.gm.daily_calls
                    + live.gm.dialogue_floor,
                )
            return ApiLimits(
                scope=scope,
                version=live.budget_version,
                attempt_limit=live.attempt_limit,
                minimum_limit=minimum,
                period=live.attempt_period,
            )

    async def result(
        self, authority: ControlAuthority, request: EditCommand
    ) -> ApiLimitChange | None:
        async with self.lock:
            live = self.config(request.scope).live
            assert live is not None
            row = live.budget_receipt
            if (
                row is None
                or row.request_id != request.request_id
                or row.operator_id != authority.principal_id
                or row.fingerprint != command_fingerprint(request)
            ):
                return None
            # Also reconcile a file commit whose acknowledgement was interrupted
            # before the in-memory admission limit was updated.
            self.rt.authorize_configuration(authority, request.scope)
            budget = self.rt.private_api_budget
            if budget is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            self.rt.private_api_budget = replace(budget, max_attempts=live.attempt_limit)
            return row

    async def apply(
        self, authority: ControlAuthority, request: EditCommand, limit: int
    ) -> ApiLimitChange:
        async with self.lock:
            config = self.config(request.scope)
            live = config.live
            assert live is not None
            if live.budget_version != request.expected_version:
                raise ContractError(
                    ErrorCode.VERSION_CONFLICT, request.request_id, live.budget_version
                )
            self.rt.authorize_configuration(authority, request.scope)
            if self.rt.supervisor.snapshot.state is not RunState.STOPPED:
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            budget = self.rt.private_api_budget
            if budget is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            now = self.rt.supervisor.clock.utc()
            row = ApiLimitChange(
                id=derived_id("budget-change", request.scope, request.request_id),
                scope=request.scope,
                version=1,
                created_at=now,
                updated_at=now,
                operator_id=authority.principal_id,
                request_id=request.request_id,
                fingerprint=command_fingerprint(request),
                attempt_limit=limit,
                period=live.attempt_period,
                budget_version=live.budget_version + 1,
                expires_at=now + self.rt.store.receipt_retention,
            )
            data = config.model_dump(mode="json")
            data["live"].update(
                attempt_limit=limit,
                budget_version=row.budget_version,
                budget_receipt=row.model_dump(mode="json"),
            )
            try:
                updated = HostConfig.model_validate_json(json.dumps(data))
                applied = replace(budget, max_attempts=limit)
            except ValueError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            # No await between the final authority check, atomic file write
            # and in-memory admission update.
            self.rt.authorize_configuration(authority, request.scope)
            save_config(self.host, updated)
            self.rt.private_api_budget = applied
            return row
