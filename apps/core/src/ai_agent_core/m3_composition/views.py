"""Panel projections: explicit scopes plus current record ACLs, never owner-wide reads."""

from dataclasses import asdict

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.persona import PersonaService
from ai_agent_application.persona.markdown import render_markdown
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.api import CommandBase, EditCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Contract, Scope
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
        if resource == "private-api-status" and identifier is None:
            from ai_agent_application.usage import UsageLedger

            now = self.runtime.supervisor.clock.utc()
            budget = self.runtime.private_api_budget if scope.memory_mode == "work" else None
            used = 0
            if budget is not None:
                rows = await UsageLedger(
                    self.runtime.store, PRINCIPALS[Owner.LLM], self.runtime.registry
                ).attempts(scope)
                used = budget.used(rows, now)
            return PrivateApiStatus(
                scope=scope,
                enabled=budget is not None,
                period=None if budget is None else budget.period,
                attempt_limit=None if budget is None else budget.max_attempts,
                attempts_used=used,
                attempts_remaining=None if budget is None else max(0, budget.max_attempts - used),
                resets_at=None if budget is None else budget.reset_at(now),
                checked_at=now,
            )
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
