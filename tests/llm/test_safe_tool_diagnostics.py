"""Rejected tool calls retain their failure and reveal only fixed technical codes."""

import logging
from dataclasses import replace

import pytest
from ai_agent_adapters.llm import ScriptedProvider
from ai_agent_application.llm import Finished, ToolCallDelta, ToolDefinition
from llm_support import gateway, request, run


@pytest.mark.parametrize(
    "event,reason,code",
    [
        (
            ToolCallDelta(0, "synthetic", "private_undeclared_name", "{}"),
            "tool_calls",
            "TOOL_UNDECLARED_NAME",
        ),
        (
            ToolCallDelta(0, "synthetic", "read_file", '{"private-text":'),
            "tool_calls",
            "TOOL_ARGUMENTS_INVALID",
        ),
        (ToolCallDelta(1, "synthetic", "read_file", "{}"), "tool_calls", "TOOL_INDICES"),
        (ToolCallDelta(0, "synthetic", "read_file", "{}"), "stop", "TOOL_TERMINAL_MISMATCH"),
    ],
)
def test_failure_log_has_fixed_code_without_model_input(caplog, event, reason, code):
    provider = ScriptedProvider([[event, Finished(reason)]])
    call = replace(
        request(),
        tools=(
            ToolDefinition(
                name="read_file",
                parameters_json='{"type":"object"}',
            ),
        ),
    )
    with caplog.at_level(logging.WARNING):
        result = run(gateway(provider).generate(call))
    assert result.status != "completed" and result.text is None and result.tool_calls == ()
    assert len(provider.calls) == 1
    assert code in caplog.text and "failure=" in caplog.text
    assert "private_undeclared_name" not in caplog.text
    assert "private-text" not in caplog.text
    assert "synthetic" not in caplog.text
