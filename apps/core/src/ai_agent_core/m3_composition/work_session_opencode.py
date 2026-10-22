"""Explicit trusted wiring of a pinned OpenCode runner into the existing session driver.

Does not prepare a host, register capabilities, grant rights or start an agent.
The launcher supplies its verified template and the operator-selected output area.
"""

from typing import TYPE_CHECKING, Literal

from ai_agent_adapters.agent_harness import OpenCodeHarness
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import ModelGateway, WorkAdmission, WorkspaceGateway
from ai_agent_application.control.ports import ResourceTicket
from ai_agent_contracts.agent_workplace import WRITABLE_AREAS
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .work_session_runtime import Release
from .workplace import Attempts

if TYPE_CHECKING:
    from .service import Execution, Runtime


class OpenCodeSessions:
    def __init__(self, runtime: Runtime, template: RunnerTemplate, *, output_area: str) -> None:
        if (
            template.executable_prefix != PREFIX
            or template.model_role_id != "activity"
            or output_area not in WRITABLE_AREAS
        ):
            raise ValueError("SESSION_RUNNER_TEMPLATE_INVALID")
        if runtime.workplace is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        self.rt, self.template, self.output_area = runtime, template, output_area
        # Live handles only, not another durable job/session/resource journal.
        self.runners: dict[str, DockerTaskRunner] = {}
        self.resources: dict[str, ResourceTicket] = {}

    def install(self) -> None:
        if self.rt.session_harness is not None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        self.rt.session_harness = self.create
        self.rt.session_capability = self.template.id
        self.rt.session_output_area = self.output_area

    async def create(
        self, execution: Execution, models: ModelGateway, admission: WorkAdmission
    ) -> OpenCodeHarness:
        from .activity import tools

        rt = self.rt
        if (
            execution.runtime is not rt
            or execution.args.request.work_session_id is None
            or execution.command.operation_id != admission.operation_id
            or execution.command.scope != admission.scope
            or admission.attempt_id in self.runners
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        place = rt.workplace
        if place is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)

        async def authorize(gate: WorkAdmission, capability: str) -> None:
            if gate != admission or capability != self.template.id:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            # The runner polls this fence every 100 ms, including DURING model IO.
            # Full JobOwner proof stays at create/model/file boundaries; acquiring its
            # lock again here can contend with that same admitted operation.
            execution.guard()
            if not any(
                tool.available and tool.manifest.capability_id == capability
                for tool in await tools(rt, gate.scope, session=True)
            ):
                raise ContractError(ErrorCode.ACCESS_DENIED)

        async def lifecycle(
            gate: WorkAdmission, name: str, phase: Literal["register", "removed", "unknown"]
        ) -> None:
            if gate != admission:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if phase == "register":
                await authorize(gate, self.template.id)
                token = rt.supervisor.resolve(
                    execution.command.cancellation_id,
                    scope=gate.scope,
                    freshness=gate.freshness,
                )
                self.resources[name] = rt.supervisor.register(token, name)
            elif phase == "removed":
                ticket = self.resources.pop(name, None)
                if ticket is None:
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                # A real Docker removal receipt is accepted even after STOP/revocation.
                rt.supervisor.acknowledge(ticket)
            else:
                # No receipt: retain the resource/runner for trusted reconciliation.
                rt.supervisor.critical_failure(ErrorCode.OUTCOME_UNKNOWN)

        runner = DockerTaskRunner(
            (self.template,),
            place.files,
            Attempts(rt),
            Release(rt),
            authorize=authorize,
            lifecycle=lifecycle,
            models=models,
            workspace=self._workspace(execution, admission)
            if self.template.max_workspace_calls
            else None,
        )
        await execution.verify()
        await authorize(admission, self.template.id)
        self.runners[admission.attempt_id] = runner
        controller = self

        class TrackedHarness(OpenCodeHarness):
            async def close(self, session_key: str) -> None:
                await super().close(session_key)
                if session_key == execution.args.request.work_session_id:
                    controller.runners.pop(admission.attempt_id, None)

        return TrackedHarness(runner, self.template.id, self.template.ceiling)

    @staticmethod
    def _workspace(execution: Execution, admission: WorkAdmission) -> WorkspaceGateway:
        from .work_session_workspace import Workspace

        return Workspace(execution, admission)
