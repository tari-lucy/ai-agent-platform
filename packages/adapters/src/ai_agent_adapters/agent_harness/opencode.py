"""AgentHarness over a trusted pinned OpenCode image and the existing bounded TaskRunner."""

import json
from typing import Literal

from ai_agent_application.agent_workplace.ports import (
    HarnessEvent,
    HarnessTurn,
    RunnerLimits,
    RunnerOutput,
    RunnerTask,
    TaskRunner,
    WorkAdmission,
)
from ai_agent_application.workspace_files.guard import current
from ai_agent_contracts.agent_workplace import ToolStep
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .worker import MAX_INSTRUCTION, MAX_STEPS, MAX_TEXT

PREFIX = ("/usr/local/bin/python", "-I", "/opt/agent-harness/worker.py")


def journal(row: dict[str, object]) -> tuple[tuple[ToolStep, ...], int]:
    """The worker's clipped journal; absent from images built before it (no steps then)."""
    steps, omitted = row.get("steps", []), row.get("steps_omitted", 0)
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError()
    if type(omitted) is not int or omitted < 0:
        raise ValueError()
    return tuple(ToolStep.model_validate(step) for step in steps), omitted


class OpenCodeHarness:
    """No durable raw cache. The driver supplies a freshly released logical-session context.

    Tools run in the sandbox; staged files still require a separate file-owner write.
    Neither the runner's exit nor OpenCode's local token counter is delivery or budget proof.
    """

    def __init__(self, runner: TaskRunner, template_id: str, limits: RunnerLimits) -> None:
        self.runner, self.template_id, self.limits = runner, template_id, limits
        self._active: tuple[str, WorkAdmission] | None = None
        self._last: tuple[str, WorkAdmission] | None = None

    async def turn(self, admission: WorkAdmission, turn: HarnessTurn) -> list[HarnessEvent]:
        current(admission)
        if self._active is not None:
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        if (
            not turn.session_key
            or not turn.instruction.strip()
            or len(turn.instruction) > MAX_INSTRUCTION
            or turn.skill_ids
        ):
            # Skills must be released/staged as real files, not resolved by an ambient directory.
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        self._active = turn.session_key, admission
        self._last = self._active
        try:
            chunks = tuple(
                turn.instruction[i : i + 8000] for i in range(0, len(turn.instruction), 8000)
            )
            result = await self.runner.run(
                admission,
                RunnerTask(
                    self.template_id, (*PREFIX, *chunks), turn.files, self.limits, network="gateway"
                ),
            )
            current(admission)
            if result.status != "succeeded":
                kind: Literal["cancelled", "unknown"] = (
                    "cancelled" if result.status == "cancelled" else "unknown"
                )
                return [HarnessEvent(kind, "SANDBOX_" + result.status.upper())]
            try:
                row = json.loads(result.stdout_tail)
                if not isinstance(row, dict):
                    raise ValueError()
                steps, omitted = journal(row)
                if row.get("status") != "completed":
                    return [
                        HarnessEvent(
                            "failed", "OPENCODE_FAILED", steps=steps, steps_omitted=omitted
                        )
                    ]
                text = row.get("text")
                errors = row.get("tool_errors", [])
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > MAX_TEXT
                    or not isinstance(errors, list)
                    or any(tool not in {"read", "write", "edit", "bash", "tool"} for tool in errors)
                ):
                    raise ValueError()
                events = [
                    HarnessEvent("progress", "SANDBOX_TOOL_FAILED:" + tool) for tool in errors
                ]
                events.append(
                    HarnessEvent(
                        "result", text, files=result.outputs, steps=steps, steps_omitted=omitted
                    )
                )
                return events
            except ValueError, TypeError, RecursionError:
                return [HarnessEvent("failed", "OPENCODE_RESULT_INVALID")]
        finally:
            self._active = None

    async def cancel(self, session_key: str) -> None:
        active = self._active
        if active is not None and active[0] == session_key:
            await self.runner.cancel(active[1])

    async def staged(self, admission: WorkAdmission, output: RunnerOutput) -> bytes:
        current(admission)
        if self._last is None or self._last[1] != admission:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        return await self.runner.staged(admission, output)

    async def close(self, session_key: str) -> None:
        # Per-turn container teardown removes OpenCode's DB/logs/config/history.
        await self.cancel(session_key)
        if self._active is None and self._last is not None and self._last[0] == session_key:
            await self.runner.cancel(self._last[1])
            self._last = None
