"""Bounded provider-neutral tool descriptions and completed proposals, never executors."""

import json
from typing import Any, Self

from ai_agent_contracts.common import Contract
from pydantic import Field, model_validator

MAX_TOOLS = 64
MAX_TOOL_CALLS = 32
MAX_TOOL_JSON = 64000


def object_json(value: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, entry in items:
            if key in result:
                raise ValueError("TOOL_JSON_DUPLICATE")
            result[key] = entry
        return result

    def constant(_: str) -> Any:
        raise ValueError("TOOL_JSON_NONFINITE")

    if len(value.encode()) > MAX_TOOL_JSON:
        raise ValueError("TOOL_JSON_LIMIT")
    parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(parsed, dict):
        raise ValueError("TOOL_JSON_OBJECT")
    # Also rejects numeric overflow (e.g. 1e999) and deeply nested documents.
    json.dumps(parsed, allow_nan=False)
    return parsed


class ToolDefinition(Contract):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=4000)
    # Immutable string: no shared mutable schema can change while a request waits.
    parameters_json: str = Field(max_length=MAX_TOOL_JSON)

    @model_validator(mode="after")
    def parameters(self) -> Self:
        if object_json(self.parameters_json).get("type") != "object":
            raise ValueError("TOOL_PARAMETERS_OBJECT")
        return self


class ToolCall(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    arguments: str = Field(max_length=MAX_TOOL_JSON)

    @model_validator(mode="after")
    def arguments_object(self) -> Self:
        object_json(self.arguments)
        return self


class ToolBuffer:
    """Unexecutable stream fragments. Only finish returns fully validated proposals."""

    def __init__(self) -> None:
        self.parts: dict[int, list[str]] = {}

    def add(self, index: int, identifier: str, name: str, arguments: str) -> None:
        if (
            type(index) is not int
            or not 0 <= index < MAX_TOOL_CALLS
            or not all(isinstance(value, str) for value in (identifier, name, arguments))
        ):
            raise ValueError("TOOL_FRAGMENT_SHAPE")
        target = self.parts.setdefault(index, ["", "", ""])
        for position, value in enumerate((identifier, name, arguments)):
            target[position] += value
        if len(target[0]) > 128 or len(target[1]) > 64 or len(target[2].encode()) > MAX_TOOL_JSON:
            raise ValueError("TOOL_FRAGMENT_LIMIT")
        if sum(sum(map(len, part)) for part in self.parts.values()) > 200000:
            raise ValueError("TOOL_OUTPUT_LIMIT")

    def finish(self, allowed: set[str]) -> tuple[ToolCall, ...]:
        if not self.parts or set(self.parts) != set(range(len(self.parts))):
            raise ValueError("TOOL_INDICES")
        result = tuple(
            ToolCall(id=part[0], name=part[1], arguments=part[2])
            for _, part in sorted(self.parts.items())
        )
        if len({call.id for call in result}) != len(result) or any(
            call.name not in allowed for call in result
        ):
            raise ValueError("TOOL_UNDECLARED_OR_DUPLICATE")
        return result
