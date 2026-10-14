"""Trusted task-local owner write fences; no transport, policy grants or new admission."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from functools import wraps
from typing import Concatenate, Protocol

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode


class WriteFenced(Protocol):
    def _write_fence(self) -> AbstractContextManager[None]: ...


def admission_guard(admitted: Callable[[], bool]) -> Callable[[], None]:
    def check() -> None:
        try:
            allowed = admitted() is True
        except Exception:
            allowed = False
        if not allowed:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    return check


def fenced[T: WriteFenced, **P, R](
    operation: Callable[Concatenate[T, P], Awaitable[R]],
) -> Callable[Concatenate[T, P], Awaitable[R]]:
    @wraps(operation)
    async def run(self: T, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._write_fence():
            return await operation(self, *args, **kwargs)

    return run
