"""One-use results of a successful JobOwner proof, local to one verification task.

This is not an admission cache or a port available to the executor/model. It only
lets composition use bindings/limits which Policy.check has just validated for
the exact command, instead of immediately querying those same records again.
"""

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass

from ai_agent_application.jobs.ports import ExecutionPolicy
from ai_agent_application.llm import ResolvedAssignment
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.control import ExecutionCommand
from ai_agent_contracts.states import ErrorCode

from .schemas import PermitLimits


@dataclass(frozen=True)
class CheckedAction:
    bindings: Mapping[str, ResolvedAssignment]
    limits: PermitLimits


class ActionRead:
    def __init__(self, command: ExecutionCommand) -> None:
        self.command = command.model_copy(deep=True)
        self.task = asyncio.current_task()
        self.value: CheckedAction | None = None
        self.proof: ExecutionPolicy | None = None
        self.consumed = False

    def record(
        self, command: ExecutionCommand, proof: ExecutionPolicy, value: CheckedAction
    ) -> None:
        # Child tasks may inherit ContextVars; their proof never belongs to this action.
        if asyncio.current_task() is not self.task or command != self.command:
            return
        if self.consumed or self.value is not None:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.value, self.proof = value, proof

    def take(self, command: ExecutionCommand, now: float) -> CheckedAction:
        if (
            self.consumed
            or asyncio.current_task() is not self.task
            or command != self.command
            or self.value is None
            or self.proof is None
            or self.proof.command_id != command.id
            or self.proof.scope != command.scope
            or not math.isfinite(now)
            or not math.isfinite(self.proof.expires_at)
            or now >= self.proof.expires_at
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        self.consumed = True
        value = self.value
        self.value = None
        return value

    def close(self) -> None:
        self.consumed = True
        self.value, self.proof = None, None
