"""M3 mutations after the existing authenticated session / Origin / CSRF boundary."""

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.catalogue import catalogue
from ai_agent_application.persona import PersonaService
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import (
    AcknowledgeCommand,
    CommandBase,
    CreateCommand,
    EditCommand,
    MessageCommand,
    Receipt,
    ReflectCommand,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.panel import (
    LaboratoryCreateArguments,
    LaboratoryStartArguments,
    SettingsTransferArguments,
)
from ai_agent_contracts.states import ErrorCode

from .schemas import ConfigureGM, TaskInput
from .service import Runtime
from .state import PRINCIPALS


class Mutations:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def execute(self, authority: ControlAuthority, request: CommandBase) -> Receipt:
        runtime = self.runtime
        runtime.authorize_configuration(authority, request.scope)
        if isinstance(request, CreateCommand) and request.type == "laboratory.create":
            args = runtime.registry.validate(
                request.arguments.schema_id,
                request.arguments.schema_version,
                request.arguments.data,
            )
            if (
                not isinstance(args, LaboratoryCreateArguments)
                or request.target != "laboratory"
                or args.persona_id != request.scope.persona_id
            ):
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            from .lab_scopes import prepare

            return await prepare(runtime, authority, request)
        if isinstance(request, (MessageCommand, AcknowledgeCommand, ReflectCommand)):
            from .dialogue import mutate

            return await mutate(runtime, authority, request)
        if isinstance(request, (CreateCommand, EditCommand)) and request.type in (
            "persona.create",
            "persona.copy",
            "persona.apply",
        ):
            if request.target != request.scope.persona_id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if (
                request.type in ("persona.create", "persona.copy")
                and runtime.supervisor.snapshot.state.value != "STOPPED"
            ):
                raise ContractError(ErrorCode.RESOURCE_BUSY)
            service = PersonaService(
                runtime.store,
                PRINCIPALS[Owner.PERSONA],
                runtime.registry,
                reader_access=runtime.readers,
            )
            args = runtime.registry.validate(
                request.arguments.schema_id,
                request.arguments.schema_version,
                request.arguments.data,
            )
            from ai_agent_contracts.panel import PersonaCopyArguments

            if (
                isinstance(args, PersonaCopyArguments)
                and args.source_scope.partition not in authority.partitions
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)
            with runtime.store.fence(
                lambda: runtime.authorize_configuration(authority, request.scope)
            ):
                if request.type == "persona.create" and isinstance(request, CreateCommand):
                    return await service.create(request)
                if request.type == "persona.copy" and isinstance(request, CreateCommand):
                    return await service.copy(request)
                if request.type == "persona.apply" and isinstance(request, EditCommand):
                    result = await service.apply(request)
                    if runtime.supervisor.snapshot.state.value in ("ACTIVE", "STARTING"):
                        runtime.supervisor.critical_failure(ErrorCode.STALE_CONTEXT)
                    return result
        if not isinstance(request, EditCommand):
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)

        def current() -> None:
            runtime.authorize_configuration(authority, request.scope)

        args = runtime.registry.validate(
            request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
        )
        with runtime.store.fence(current):
            if request.type == "gm.configure" and isinstance(args, ConfigureGM):
                if (
                    request.target != "gm-runtime"
                    or request.arguments.schema_id != "gm.configure-args"
                ):
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                # Pausing is synchronous, before storage IO. Unpause follows a committed edit.
                if args.paused is True and not runtime.supervisor.snapshot.gm_paused:
                    runtime.supervisor.set_gm(
                        authority, True, expected_version=runtime.supervisor.snapshot.version
                    )
                result = await runtime.state.configure(request, args)
                if args.rules is not None:
                    runtime.supervisor.critical_failure(ErrorCode.STALE_CONTEXT)
                elif args.paused is False and runtime.supervisor.snapshot.gm_paused:
                    current()
                    runtime.supervisor.set_gm(
                        authority, False, expected_version=runtime.supervisor.snapshot.version
                    )
                return result
            if request.type == "laboratory.start" and isinstance(args, LaboratoryStartArguments):
                if request.expected_version != runtime.supervisor.snapshot.version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                if (
                    request.target != "laboratory"
                    or request.scope != runtime.supervisor.snapshot.scope
                ):
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                selected = next((s for s in catalogue() if s.scenario_id == args.scenario_id), None)
                if selected is None or selected.version != args.scenario_version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                job = await runtime.submit(
                    authority,
                    request.request_id,
                    TaskInput(
                        kind="laboratory",
                        trigger_id=request.request_id,
                        scenario_id=args.scenario_id,
                        lab_scope=args.lab_scope,
                        seed=args.seed,
                    ),
                )
                return Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="accepted",
                    operation_id=job.operation_id,
                    version=job.version,
                )
            if request.type == "laboratory.transfer_settings" and isinstance(
                args, SettingsTransferArguments
            ):
                from .transfers import apply

                async with runtime._lock:
                    return await apply(runtime, authority, request)
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
