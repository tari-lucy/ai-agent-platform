"""Complete Chat Completions tool arrays: same gateway validation, no SSE index guessing."""

import json

import httpx
import pytest
from ai_agent_application.llm import LLMGateway
from llm_support import resolved, run
from test_openai_adapter import provider_for
from test_tools import tool_request


def document():
    return {
        "model": "up-model-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "one",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"synthetic"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8},
    }


@pytest.mark.parametrize(
    "change,ok",
    [
        ("none", True),
        ("unknown_name", False),
        ("broken_json", False),
        ("empty", False),
        ("wrong_finish", False),
        ("two_choices", False),
    ],
)
def test_complete_tools_remain_closed_and_usage_is_preserved(change, ok):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["stream"] is False and "stream_options" not in body and body["tools"]
        value = document()
        message = value["choices"][0]["message"]
        if change == "unknown_name":
            message["tool_calls"][0]["function"]["name"] = "undeclared"
        if change == "broken_json":
            message["tool_calls"][0]["function"]["arguments"] = "{"
        if change == "empty":
            message["tool_calls"] = []
        if change == "wrong_finish":
            value["choices"][0]["finish_reason"] = "stop"
        if change == "two_choices":
            value["choices"].append(value["choices"][0])
        return httpx.Response(200, json=value)

    provider = provider_for(handler)
    provider.stream_tools = False
    result = run(LLMGateway(lambda _: provider, lambda _: resolved()).generate(tool_request()))
    assert len(calls) == 1
    if ok:
        assert result.status == "completed" and len(result.tool_calls) == 1
        assert json.loads(result.tool_calls[0].arguments) == {"path": "synthetic"}
        assert result.attempts[0].usage.output_tokens == 8
    else:
        assert result.status != "completed" and result.tool_calls == () and result.text is None


def test_complete_final_text_is_allowed_when_no_tool_is_selected():
    def handler(_):
        value = document()
        value["choices"][0] = {"message": {"content": "Synthetic done"}, "finish_reason": "stop"}
        return httpx.Response(200, json=value)

    provider = provider_for(handler)
    provider.stream_tools = False
    result = run(LLMGateway(lambda _: provider, lambda _: resolved()).generate(tool_request()))
    assert (
        result.status == "completed" and result.text == "Synthetic done" and not result.tool_calls
    )
