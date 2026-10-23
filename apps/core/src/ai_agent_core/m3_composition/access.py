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
    ExecutePlanCommand,
    MessageCommand,
    PlanCommand,
    ProcessMemoryCommand,
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
        inactive_edits = {
            # Durable intake into an already authorised/prepared partition is metadata,
            # not a second runtime. The selector still executes only the active partition.
            "work.session.open",
            "work.session.message",
            "work.session.close",
            "persona.create",
            "persona.copy",
            "persona.apply",
            "gm.configure",
            "change.decision",
            "change.interpretation",
            "change.rollback.preview",
            "change.rollback.execute",
            "opportunity.cancel",
            "arc.pause",
            "memory.correct",
            "memory.delete.preview",
            "memory.delete.execute",
            "memory.export.preview",
            "memory.export.execute",
            "tariff.create",
            "llm.limits",
        }
        if request.scope.partition != runtime.supervisor.snapshot.scope.partition and (
            not isinstance(request, (CreateCommand, EditCommand, PlanCommand, ExecutePlanCommand))
            or request.type not in inactive_edits
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
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
        if isinstance(request, ProcessMemoryCommand):
            from .memory_cycles import manual

            return await manual(runtime, authority, request)
        if isinstance(request, CreateCommand) and request.type == "tariff.create":
            from .llm_controls import tariff

            async with runtime._lock:
                return await tariff(runtime, authority, request)
        if isinstance(request, EditCommand) and request.type == "llm.limits":
            from .llm_controls import limits

            async with runtime._lock:
                return await limits(runtime, authority, request)
        if isinstance(request, (PlanCommand, ExecutePlanCommand)) and request.type in (
            "memory.delete.preview",
            "memory.delete.execute",
            "memory.export.preview",
            "memory.export.execute",
        ):
            from .memory_lifecycle import execute, preview

            async with runtime._lock:
                return await (
                    preview(runtime, authority, request)
                    if isinstance(request, PlanCommand)
                    else execute(runtime, authority, request)
                )
        if isinstance(request, EditCommand) and request.type == "memory.correct":
            from .memory_controls import correct

            async with runtime._lock:
                return await correct(runtime, authority, request)
        if isinstance(request, EditCommand) and request.type == "promise.close":
            from .social_controls import close

            async with runtime._lock:
                return await close(runtime, authority, request)
        if isinstance(request, (PlanCommand, ExecutePlanCommand)) and request.type in (
            "permission.decision.preview",
            "permission.decision.execute",
        ):
            from .permission_decisions import execute as decide
            from .permission_decisions import preview as propose

            if runtime.permission_ceiling is None:
                raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
            async with runtime._lock:
                return await (
                    propose(runtime, authority, request)
                    if isinstance(request, PlanCommand)
                    else decide(runtime, authority, request)
                )
        if isinstance(request, CreateCommand) and request.type == "work.assign":
            from .activity import assign

            async with runtime._lock:
                return await assign(runtime, authority, request)
        if isinstance(request, (CreateCommand, EditCommand)) and request.type in (
            "work.session.open",
            "work.session.message",
            "work.session.close",
        ):
            from .work_sessions import mutate as session_mutate

            async with runtime._lock:
                return await session_mutate(runtime, authority, request)
        if isinstance(request, EditCommand) and request.type == "work.abandon":
            from .activity import abandon

            async with runtime._lock:
                return await abandon(runtime, authority, request)
        if isinstance(request, EditCommand) and request.type in ("ticket.reply", "ticket.read"):
            from .tickets import mutate as ticket_mutate

            return await ticket_mutate(runtime, authority, request)
        if isinstance(request, AcknowledgeCommand) and request.target.startswith("work-reply-"):
            from .work_session_delivery import acknowledge

            async with runtime._lock:
                return await acknowledge(runtime, authority, request)
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
                    if (
                        request.scope.partition == runtime.supervisor.snapshot.scope.partition
                        and runtime.supervisor.snapshot.state.value in ("ACTIVE", "STARTING")
                    ):
                        runtime.supervisor.critical_failure(ErrorCode.STALE_CONTEXT)
                    return result
        if isinstance(request, PlanCommand) and request.type == "change.rollback.preview":
            from .development_controls import preview

            async with runtime._lock:
                return await preview(runtime, authority, request)
        if isinstance(request, (EditCommand, ExecutePlanCommand)) and request.type in (
            "change.rollback.execute",
            "change.decision",
            "change.interpretation",
            "opportunity.cancel",
            "arc.pause",
        ):
            from .development_controls import mutate as development_mutate

            async with runtime._lock:
                return await development_mutate(runtime, authority, request)
        if isinstance(request, EditCommand) and request.type in (
            "laboratory.stop",
            "laboratory.event",
        ):
            return await runtime.lab_control.mutate(authority, request)
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
                if (
                    args.paused is not None
                    and request.scope.partition != runtime.supervisor.snapshot.scope.partition
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                # Pausing is synchronous, before storage IO. Unpause follows a committed edit.
                if args.paused is True and not runtime.supervisor.snapshot.gm_paused:
                    runtime.supervisor.set_gm(
                        authority, True, expected_version=runtime.supervisor.snapshot.version
                    )
                result = await runtime.state.configure(request, args)
                if (
                    args.rules is not None
                    and request.scope.partition == runtime.supervisor.snapshot.scope.partition
                ):
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
