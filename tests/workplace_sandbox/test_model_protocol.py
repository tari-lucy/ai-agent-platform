"""Closed tool request wire, before a call can spend shared budget."""

from copy import deepcopy

import pytest
from ai_agent_adapters.sandbox.model_protocol import model_request

ROW = {
    "kind": "model",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "test"}],
    "tools": [
        {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
    ],
}


def parse(row, **limits):
    return model_request(
        row,
        role="activity",
        max_tokens=64,
        max_prompt=limits.get("size", 1000),
        call_id="sandbox-1",
    )


def test_tool_payload_uses_bound_role_and_closed_history():
    row = deepcopy(ROW)
    row["messages"] += [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "synthetic contents"},
    ]
    result = parse(row)
    assert result.role_id == "activity" and result.max_output_tokens == 32
    assert result.messages[-2].content == "" and result.messages[-1].tool_call_id == "a"
    assert result.tools[0].name == "read_file"
    with pytest.raises(ValueError, match="RUNNER_PROMPT_LIMIT"):
        parse(row, size=30)


@pytest.mark.parametrize(
    "change",
    [
        {"role": "foreign"},
        {"max_tokens": True},
        {"max_tokens": 65},
        {"tools": [{"type": "host-shell", "function": {}}]},
        {
            "tools": [
                {"type": "function", "function": {"name": "x", "parameters": [], "secret": "x"}}
            ]
        },
        {"messages": [{"role": "tool", "content": "orphan", "tool_call_id": "x"}]},
        {"messages": [{"role": "user", "content": "test", "source_id": "fake"}]},
    ],
)
def test_invalid_payload_cannot_reach_gateway(change):
    with pytest.raises((ValueError, TypeError)):
        parse(dict(ROW, **change))
