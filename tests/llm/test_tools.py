"""Complete tool proposals over the existing gateway; synthetic, no tool execution."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_adapters.llm.mock import Pause
from ai_agent_application.llm import (
    ChatMessage,
    Failure,
    Finished,
    LLMGateway,
    ProviderError,
    ToolCall,
    ToolCallDelta,
    ToolDefinition,
    UsageReported,
)
from ai_agent_application.llm.model import prompt_size
from llm_support import gateway, request, resolved, run, usage
from test_openai_adapter import chunk, ok, provider_for, sse

TOOL = ToolDefinition(name="read_file", parameters_json='{"type":"object"}')


def tool_request(**changes):
    return replace(request(), tools=(TOOL,), **changes)


def test_interleaved_fragments_are_released_only_as_complete_named_calls_with_usage():
    provider = ScriptedProvider(
        [
            [
                ToolCallDelta(0, "call_a", "read_", '{"path":'),
                ToolCallDelta(1, "call_b", "read_file", "{}"),
                ToolCallDelta(0, name="file", arguments='"notes"}'),
                Finished("tool_calls"),
                UsageReported(usage(20, 8)),
            ]
        ]
    )
    result = run(gateway(provider).generate(tool_request()))
    assert result.status == "completed" and result.text == ""
    assert [(call.id, call.name, json.loads(call.arguments)) for call in result.tool_calls] == [
        ("call_a", "read_file", {"path": "notes"}),
        ("call_b", "read_file", {}),
    ]
    assert result.attempts[0].usage.output_tokens == 8
    assert provider.calls[0].tools == (TOOL,)


@pytest.mark.parametrize(
    "events",
    [
        [ToolCallDelta(0, "a", "read_file", "{"), Finished("tool_calls")],
        [ToolCallDelta(0, "a", "undeclared", "{}"), Finished("tool_calls")],
        [ToolCallDelta(0, "a", "read_file", '{"x":1,"x":2}'), Finished("tool_calls")],
        [ToolCallDelta(0, "a", "read_file", '{"x":NaN}'), Finished("tool_calls")],
        [ToolCallDelta(1, "a", "read_file", "{}"), Finished("tool_calls")],
        [
            ToolCallDelta(0, "a", "read_file", "{}"),
            ToolCallDelta(1, "a", "read_file", "{}"),
            Finished("tool_calls"),
        ],
        [ToolCallDelta(0, "a", "read_file", "{}"), Finished("length")],
        [ToolCallDelta(0, "a", "read_file", "{}"), Finished("stop")],
        [ToolCallDelta(0, "a", "read_file", "{}"), ProviderError(Failure.TRANSIENT)],
        [Finished("tool_calls")],
    ],
)
def test_malformed_or_partial_calls_never_escape_or_retry(events):
    provider = ScriptedProvider([events])
    result = run(gateway(provider).generate(tool_request()))
    assert result.status != "completed" and result.tool_calls == () and result.text is None
    assert len(provider.calls) == 1


def test_stop_during_tool_stream_keeps_technical_receipt_and_discards_proposal():
    async def scenario():
        cancel = asyncio.Event()
        provider = ScriptedProvider(
            [
                [
                    ToolCallDelta(0, "a", "read_file", "{}"),
                    Pause(10),
                    Finished("tool_calls"),
                ]
            ]
        )
        task = asyncio.create_task(gateway(provider).generate(tool_request(), cancel))
        await asyncio.sleep(0.01)
        cancel.set()
        result = await task
        assert result.status == "cancelled" and result.tool_calls == ()
        assert len(result.attempts) == 1 and provider.closed == 1

    run(scenario())


def test_tool_history_requires_exact_result_pairing_and_counts_all_payloads():
    call = ToolCall(id="a", name="read_file", arguments='{"path":"notes"}')
    messages = (
        *request().messages,
        ChatMessage("assistant", "", (call,)),
        ChatMessage("tool", "file contents", tool_call_id="a"),
    )
    valid = tool_request(messages=messages)
    assert prompt_size(valid.messages, valid.tools) > sum(len(m.content) for m in messages)
    for history in (
        messages[:-1],
        (*messages, messages[-1]),
        (ChatMessage("tool", "orphan", tool_call_id="x"),),
        (*messages[:-1], ChatMessage("user", "skip result")),
    ):
        with pytest.raises(ValueError):
            tool_request(messages=history)
    with pytest.raises(ValueError, match="TOOLS_BOUNDS_OR_DUPLICATE"):
        replace(request(), tools=(TOOL, TOOL))


def test_http_adapter_serializes_history_and_collects_streamed_tools():
    seen = []

    def handler(http_request):
        seen.append(json.loads(http_request.content))
        first = chunk()
        first["choices"][0]["delta"] = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }
            ]
        }
        second = chunk(finish="tool_calls")
        second["choices"][0]["delta"] = {
            "tool_calls": [{"index": 0, "function": {"arguments": '"next"}'}}]
        }
        return ok(
            sse(
                first,
                second,
                chunk(usage={"prompt_tokens": 20, "completion_tokens": 8}, choices=False),
            )
        )(http_request)

    call = ToolCall(id="a", name="read_file", arguments="{}")
    req = tool_request(
        messages=(
            *request().messages,
            ChatMessage("assistant", "", (call,)),
            ChatMessage("tool", "old result", tool_call_id="a"),
        )
    )
    provider = provider_for(handler)
    result = run(LLMGateway(lambda _: provider, lambda _: resolved()).generate(req))
    assert result.status == "completed" and result.tool_calls[0].id == "b"
    assert json.loads(result.tool_calls[0].arguments) == {"path": "next"}
    body = seen[0]
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert body["messages"][-1]["tool_call_id"] == "a"
    assert body["messages"][-2]["tool_calls"][0]["function"]["arguments"] == "{}"


@pytest.mark.parametrize(
    "fragment",
    [
        None,
        {},
        [{"index": True}],
        [{"index": 0, "function": {"arguments": {}}}],
        [{"index": 0, "type": "other"}],
    ],
)
def test_malformed_remote_tool_shape_is_protocol_failure_without_body(fragment):
    part = chunk()
    part["choices"][0]["delta"] = {"tool_calls": fragment}
    provider = provider_for(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(part)
        )
    )
    result = run(LLMGateway(lambda _: provider, lambda _: resolved()).generate(tool_request()))
    assert result.failure == Failure.PROTOCOL and result.tool_calls == ()
