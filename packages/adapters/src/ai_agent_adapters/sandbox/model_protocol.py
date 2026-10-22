"""Closed, bounded untrusted harness input. Model proposals grant no host capabilities."""

import json
from typing import Any

from ai_agent_application.agent_workplace.ports import ModelRequest, WorkspaceRequest
from ai_agent_application.llm import ChatMessage, GenerationRequest, ToolCall, ToolDefinition
from ai_agent_application.llm.model import prompt_size
from ai_agent_application.llm.tools import MAX_TOOL_CALLS, MAX_TOOLS


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or (value.keys() - required - (optional or set()))
    ):
        raise ValueError("RUNNER_MODEL_SHAPE")
    return value


def workspace_request(row: Any) -> WorkspaceRequest:
    value = _keys(row, {"kind", "call_id", "operation", "arguments"})
    arguments = value["arguments"]
    if (
        value["kind"] != "workspace"
        or not isinstance(value["call_id"], str)
        or not 1 <= len(value["call_id"]) <= 64
        or value["operation"]
        not in {
            "capabilities",
            "list_files",
            "read_file",
            "write_note",
            "raise_ticket",
            "read_ticket",
        }
        or not isinstance(arguments, dict)
        or len(arguments) > 12
        or any(
            not isinstance(k, str) or len(k) > 64 or not isinstance(v, str) or len(v) > 4000
            for k, v in arguments.items()
        )
    ):
        raise ValueError("RUNNER_WORKSPACE_REQUEST_INVALID")
    return WorkspaceRequest(value["call_id"], value["operation"], arguments)


def _calls(value: Any) -> tuple[ToolCall, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TOOL_CALLS:
        raise ValueError("RUNNER_CALLS_LIMIT")
    result = []
    for raw in value:
        call = _keys(raw, {"id", "type", "function"})
        function = _keys(call["function"], {"name", "arguments"})
        if call["type"] != "function":
            raise ValueError("RUNNER_TOOL_TYPE")
        result.append(ToolCall(id=call["id"], **function))
    return tuple(result)


def _tools(value: Any) -> tuple[ToolDefinition, ...]:
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise ValueError("RUNNER_TOOLS_LIMIT")
    result = []
    for raw in value:
        tool = _keys(raw, {"type", "function"})
        function = _keys(tool["function"], {"name", "parameters"}, {"description"})
        if tool["type"] != "function" or not isinstance(function["parameters"], dict):
            raise ValueError("RUNNER_TOOL_TYPE")
        result.append(
            ToolDefinition(
                name=function["name"],
                description=function.get("description", ""),
                parameters_json=json.dumps(function["parameters"], allow_nan=False),
            )
        )
    return tuple(result)


def model_request(
    row: Any, *, role: str, max_tokens: int, max_prompt: int, call_id: str
) -> ModelRequest:
    value = _keys(row, {"kind", "messages", "max_tokens"}, {"tools"})
    entries, tokens = value["messages"], value["max_tokens"]
    if (
        value["kind"] != "model"
        or type(tokens) is not int
        or not 1 <= tokens <= max_tokens
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 200
    ):
        raise ValueError("RUNNER_MODEL_REQUEST_INVALID")
    messages = []
    for raw in entries:
        message = _keys(raw, {"role", "content"}, {"tool_calls", "tool_call_id"})
        # Chat Completions represents an assistant tool-only reply with null content.
        content = message["content"]
        if content is None and message["role"] == "assistant" and message.get("tool_calls"):
            content = ""
        messages.append(
            ChatMessage(
                message["role"],
                content,
                _calls(message["tool_calls"]) if "tool_calls" in message else (),
                message.get("tool_call_id"),
            )
        )
    tools = _tools(value.get("tools", []))
    checked = GenerationRequest(
        operation_id=call_id,
        role_id=role,
        messages=tuple(messages),
        max_output_tokens=tokens,
        tools=tools,
    )
    if prompt_size(checked.messages, tools) > max_prompt:
        raise ValueError("RUNNER_PROMPT_LIMIT")
    return ModelRequest(role, checked.messages, tokens, call_id=call_id, tools=tools)


def wire_calls(calls: tuple[ToolCall, ...]) -> list[dict[str, Any]]:
    return [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in calls
    ]
