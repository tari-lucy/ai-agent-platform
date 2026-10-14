"""M3 mutations after the existing authenticated session / Origin / CSRF boundary."""

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.laboratory.transfer import SettingsTransfer
from ai_agent_application.persona import PersonaService
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import CommandBase, EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .schemas import ConfigureGM, TaskInput, TransferInput
from .service import Runtime
from .settings import DurableSettings, TransferJournal
from .state import PRINCIPALS


class Mutations:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def execute(self, authority: ControlAuthority, request: CommandBase) -> Receipt:
        runtime = self.runtime
        runtime.supervisor.authorize_operator(authority, request.scope)
        if not isinstance(request, EditCommand):
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)

        def current() -> None:
            runtime.supervisor.authorize_operator(authority, request.scope)

        args = runtime.registry.validate(
            request.arguments.schema_id, request.arguments.schema_version, request.arguments.data
        )
        with runtime.store.fence(current):
            if request.type == "gm.configure" and isinstance(args, ConfigureGM):
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
            if (
                request.type == "laboratory.start"
                and isinstance(args, TaskInput)
                and args.kind == "laboratory"
            ):
                if request.expected_version != runtime.supervisor.snapshot.version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                job = await runtime.submit(authority, request.request_id, args)
                return Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="accepted",
                    operation_id=job.operation_id,
                    version=job.version,
                )
            if request.type == "laboratory.transfer_settings" and isinstance(args, TransferInput):
                if args.source_scope.partition not in {s.partition for s in runtime.scopes}:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                old = await runtime.state.gm(request.scope)
                if request.expected_version != old.version:
                    raise ContractError(ErrorCode.VERSION_CONFLICT)
                from ai_agent_contracts.states import RunState

                if runtime.supervisor.snapshot.state != RunState.STOPPED:
                    raise ContractError(ErrorCode.RESOURCE_BUSY)
                journal = TransferJournal(runtime.state, clock=runtime.supervisor.clock.utc)
                if args.action == "acknowledge":
                    await journal.acknowledge(
                        request.scope, args.operation_id, authority.principal_id
                    )
                else:
                    persona = PersonaService(
                        runtime.store, PRINCIPALS[Owner.PERSONA], runtime.registry
                    )
                    transfer = SettingsTransfer(persona, DurableSettings(runtime.state))
                    if args.preview_digest is None:
                        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
                    await journal.apply(
                        transfer,
                        args.source_scope,
                        request.scope,
                        args.keys,
                        args.operation_id,
                        args.preview_digest,
                        authority.principal_id,
                    )
                current()
                return Receipt(
                    request_id=request.request_id,
                    scope=request.scope,
                    status="completed",
                    resource_id=args.operation_id,
                )
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
