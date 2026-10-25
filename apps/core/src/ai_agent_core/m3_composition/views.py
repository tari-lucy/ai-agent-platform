"""Panel projections: explicit scopes plus current record ACLs, never owner-wide reads."""

from collections.abc import Callable
from dataclasses import asdict

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.persona import PersonaService
from ai_agent_application.persona.markdown import render_markdown
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import CommandBase, EditCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Contract, Scope
from ai_agent_contracts.memory import Claim, RecallRequest, RecallResult, Source
from ai_agent_contracts.panel import (
    LaboratoryCatalogue,
    LaboratoryScenario,
    PrivateApiStatus,
    ProfileMarkdown,
    ProfileValidation,
    SettingsTransferStatus,
)
from ai_agent_contracts.persona import CharacterState, Profile, SettingsTransferOperation
from ai_agent_contracts.states import ErrorCode

from .service import Runtime
from .state import PRINCIPALS


class Views:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def recall(self, principal_id: str, request: RecallRequest) -> RecallResult:
        from ai_agent_application.memory.recall import RecallService

        rt = self.runtime
        if request.purpose != "api_transfer":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if request.scope.partition not in {s.partition for s in rt.scopes}:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if rt.dialogue_index is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        principal = Principal(principal_id, "operator")
        result = await RecallService(
            rt.store,
            principal,
            rt.dialogue_index,
            clock=rt.supervisor.clock.utc,
        ).recall(request)
        # Recheck after recall's awaited index/conflict reads; sources stay authoritative.
        fragments = []
        for item in result.fragments:
            source = await rt.store.repository(Source, principal, request.scope).get(
                item.source.source_id, purpose="api_transfer"
            )
            if source is None or source.version != item.source.version:
                raise ContractError(ErrorCode.STALE_CONTEXT)
            conflicts = []
            for identifier in item.conflicts:
                if (
                    await rt.store.repository(Claim, principal, request.scope).get(
                        identifier, purpose="api_transfer"
                    )
                    is not None
                ):
                    conflicts.append(identifier)
            fragments.append(item.model_copy(update={"conflicts": conflicts}))
        return result.model_copy(update={"fragments": fragments, "missing_source_ids": []})

    async def profile(self, principal_id: str, scope: Scope, identifier: str) -> Profile:
        if identifier != scope.persona_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        result = await self.runtime.store.repository(
            Profile, Principal(principal_id, "operator"), scope
        ).get(identifier, purpose="api_transfer")
        if result is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return result

    async def read(
        self,
        resource: str,
        principal_id: str,
        scope: Scope,
        identifier: str | None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Contract:
        if scope.partition not in {s.partition for s in self.runtime.scopes}:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if resource == "api-limits" and identifier is None:
            if self.runtime.budget_controls is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            return await self.runtime.budget_controls.read(scope)
        if resource == "private-api-status" and identifier is None:
            from ai_agent_application.usage import UsageLedger

            now = self.runtime.supervisor.clock.utc()
            budget = self.runtime.private_api_budget if scope.memory_mode == "work" else None
            used = 0
            rows = []
            if budget is not None:
                rows = await UsageLedger(
                    self.runtime.store, PRINCIPALS[Owner.LLM], self.runtime.registry
                ).attempts(scope)
                used = budget.used(rows, now)
            return PrivateApiStatus(
                scope=scope,
                enabled=budget is not None,
                memory_processing=budget is not None and budget.memory_processing,
                memory_daily_calls=budget.memory_daily_calls
                if budget and budget.memory_processing
                else 0,
                memory_calls_used=budget.memory_used(rows, now)
                if budget and budget.memory_processing
                else 0,
                activity_daily_calls=budget.activity_daily_calls
                if budget and budget.activity_enabled
                else 0,
                activity_calls_used=budget.activity_used(rows, now)
                if budget and budget.activity_enabled
                else 0,
                dialogue_remaining=budget.dialogue_remaining(rows, now) if budget else None,
                period=None if budget is None else budget.period,
                attempt_limit=None if budget is None else budget.max_attempts,
                attempts_used=used,
                attempts_remaining=None if budget is None else max(0, budget.max_attempts - used),
                resets_at=None if budget is None else budget.reset_at(now),
                checked_at=now,
            )
        if resource == "workplace-file" and identifier is not None:
            from .workplace import file_content

            return await file_content(self.runtime, scope, identifier)
        if resource == "work-session-sources" and identifier is not None:
            from .work_sessions import source_selection

            return await source_selection(self.runtime, scope, identifier)
        if resource == "work-session-transcript" and identifier is not None:
            from .work_sessions import transcript

            return await transcript(self.runtime, scope, identifier)
        if resource == "workplace-access" and identifier is None:
            from .initial_access import status

            return await status(self.runtime, scope)
        if resource == "workplace-library" and identifier is None:
            from .workplace import library

            return await library(self.runtime, scope)
        if resource == "laboratory-runs" and identifier is None:
            return await self.runtime.lab_control.read(principal_id, scope, limit, cursor)
        if resource == "settings-transfer-status" and identifier is not None:
            from ai_agent_application.memory.service import derived_id

            row = await self.runtime.store.repository(
                SettingsTransferOperation, Principal(principal_id, "operator"), scope
            ).get(derived_id("transfer", scope, identifier), purpose="api_transfer")
            if row is not None and row.operator_id != principal_id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            return SettingsTransferStatus(scope=scope, operation_id=identifier, record=row)
        if resource == "laboratory-catalogue" and identifier is None:
            return LaboratoryCatalogue(
                scope=scope,
                scenarios=[
                    LaboratoryScenario(
                        scenario_id=s.scenario_id,
                        version=s.version,
                        title=s.title,
                        tags=list(s.tags),
                    )
                    for s in catalogue()
                ],
            )
        if resource == "persona-markdown" and identifier is not None:
            profile = await self.profile(principal_id, scope, identifier)
            return ProfileMarkdown(
                scope=scope,
                profile_id=profile.id,
                version=profile.version,
                markdown=render_markdown(
                    PersonaService(
                        self.runtime.store, PRINCIPALS[Owner.PERSONA], self.runtime.registry
                    )._parsed(profile)
                ),
            )
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)

    async def validate_profile(
        self, authority: ControlAuthority, request: CommandBase
    ) -> ProfileValidation:
        rt = self.runtime
        rt.authorize_configuration(authority, request.scope)
        if not isinstance(request, EditCommand) or request.type != "persona.validate":
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        await self.profile(authority.principal_id, request.scope, request.target)
        # Validation can reveal state blockers; it requires the state's own read permission.
        state = await rt.store.repository(
            CharacterState, Principal(authority.principal_id, "operator"), request.scope
        ).get("state:" + request.scope.persona_id, purpose="api_transfer")
        if state is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        report = await PersonaService(rt.store, PRINCIPALS[Owner.PERSONA], rt.registry).validate(
            request
        )
        rt.authorize_configuration(authority, request.scope)
        return ProfileValidation.model_validate(
            dict(
                request_id=request.request_id,
                scope=request.scope,
                **{
                    k: (list(v) if isinstance(v, tuple) else v)
                    for k, v in asdict(report).items()
                    if k != "diff"
                },
                ready=report.ready,
                diff={
                    k: (list(v) if isinstance(v, tuple) else v)
                    for k, v in asdict(report.diff).items()
                }
                if report.diff is not None
                else None,
            )
        )

    async def preview_transfer(self, authority: ControlAuthority, request: CommandBase) -> Contract:
        from .transfers import preview

        if not isinstance(request, EditCommand):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        async with self.runtime._lock:
            return await preview(self.runtime, authority, request)

    async def operator_upload(
        self,
        authority: ControlAuthority,
        scope: Scope,
        area: str,
        name: str,
        title: str,
        media_type: str,
        content: bytes,
        *,
        authorize: Callable[[], None],
        request_id: str,
    ) -> Contract:
        """Trusted operator upload; the access boundary has already authenticated the session."""
        if scope.partition not in {s.partition for s in self.runtime.scopes}:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        from .workplace import operator_upload as upload

        def guard() -> None:
            authorize()
            self.runtime.authorize_configuration(authority, scope)

        with self.runtime.store.fence(guard, owner=Owner.CATALOGUE):
            guard()
            return await upload(
                self.runtime,
                scope,
                area,
                name,
                title,
                media_type,
                content,
                authorize=guard,
                request_id=request_id,
            )
