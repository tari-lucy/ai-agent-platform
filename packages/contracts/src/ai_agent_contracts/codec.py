"""Bounded, safe JSON conversion and pure concurrency/idempotency checks."""

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from ai_agent_contracts.api import ERROR_MESSAGES, ApiError, CommandBase
from ai_agent_contracts.common import MAX_BODY_BYTES, Contract, Scope
from ai_agent_contracts.control import ExecutionResult, Freshness, Lease
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ErrorCode


class ContractError(ValueError):
    def __init__(
        self, code: ErrorCode, request_id: str | None = None, current_version: int | None = None
    ) -> None:
        self.error = ApiError(
            code=code,
            message=ERROR_MESSAGES[code],
            request_id=request_id,
            current_version=current_version,
        )
        super().__init__(self.error.message)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("NONFINITE_JSON")


def _depth(value: Any, level: int = 0) -> None:
    if level > 32:
        raise ValueError("JSON_DEPTH")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


def from_wire[T](
    adapter: TypeAdapter[T], data: bytes, *, extensions: ExtensionRegistry | None = None
) -> T:
    """Full JSON only. Raw Pydantic/provider exceptions never cross this boundary."""
    if len(data) > MAX_BODY_BYTES:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant
        )
        _depth(value)
        # Re-encode to preserve Pydantic's strict JSON semantics for scalar integers,
        # while letting wire datetimes/enums have their documented string representation.
        canonical = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return adapter.validate_json(canonical, context={"extensions": extensions})
    except ValidationError, ValueError, TypeError, RecursionError, UnicodeError:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None


def to_wire(model: BaseModel, *, extensions: ExtensionRegistry | None = None) -> bytes:
    """Revalidate mutable nested values and enforce the same outbound body limit."""
    try:
        data = model.model_dump_json().encode("utf-8")
        if len(data) > MAX_BODY_BYTES:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        from_wire(TypeAdapter(type(model)), data, extensions=extensions)
        return data
    except ValidationError, ValueError, TypeError, RecursionError, UnicodeError:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None


def scoped_operation_key(command: CommandBase) -> tuple[str, str, str, str, str]:
    """One logical request namespace per data partition; type changes still conflict."""
    return (*command.scope.partition, command.request_id)


def command_fingerprint(command: CommandBase) -> str:
    value = command.model_dump(mode="json", exclude={"request_id"})
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_replay(previous: CommandBase, incoming: CommandBase) -> None:
    if previous.scope.partition != incoming.scope.partition:
        raise ContractError(ErrorCode.ACCESS_DENIED, incoming.request_id)
    if previous.request_id != incoming.request_id:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS, incoming.request_id)
    if command_fingerprint(previous) != command_fingerprint(incoming):
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT, incoming.request_id)
    # This only establishes identity. The owner MUST still check current admission.


def check_version(expected: int, current: int, request_id: str | None = None) -> None:
    if expected != current:
        raise ContractError(ErrorCode.VERSION_CONFLICT, request_id, current)


def check_result_context(
    result: ExecutionResult, *, scope: Scope, freshness: Freshness, lease: Lease, now: datetime
) -> None:
    """Pure admission predicate for business results, never for late usage reconciliation."""
    offset = now.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    if result.scope.partition != scope.partition:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if (
        result.scope != scope
        or result.freshness != freshness
        or (
            lease.scope != scope
            or lease.freshness != freshness
            or result.operation_id != lease.operation_id
            or result.command_id != lease.command_id
            or result.attempt_id != lease.attempt_id
            or result.lease_version != lease.lease_version
        )
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    if now >= lease.expires_at:
        raise ContractError(ErrorCode.EXPIRED)
    # No transaction, state transition or external effect occurs here.


def contract_adapter(model: type[Contract]) -> TypeAdapter[Contract]:
    return TypeAdapter(model)
