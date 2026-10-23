"""One accounting role, two trusted result contracts; no network or real model."""

import asyncio
from dataclasses import replace

import pytest
from ai_agent_adapters.llm import ADAPTER_ID
from ai_agent_application.agent_activity.model import StepDecision
from ai_agent_application.llm import Delta, Finished, LLMGateway, ToolCallDelta, ToolDefinition
from llm_support import request, resolved

from ai_agent_core.local_host.layout import ActivityConfig, LiveConfig
from ai_agent_core.local_host.provider import WORK_SESSION_PROTOCOL, PrivateLiveProvider


@pytest.mark.parametrize("kind", ["planner", "text", "tool"])
def test_activity_protocol_follows_gateway_result_contract(kind):
    async def scenario():
        seen = []

        class Wire:
            async def stream(self, call):
                seen.append(call)
                system = call.messages[0].content
                if kind == "planner":
                    assert call.result_model is StepDecision
                    assert '"StepDecision"' in system
                    assert WORK_SESSION_PROTOCOL not in system
                    yield Delta('{"action":"complete","summary":"Synthetic done"}')
                    yield Finished("stop")
                else:
                    assert call.result_model is None
                    assert system.endswith(WORK_SESSION_PROTOCOL)
                    assert '"StepDecision"' not in system
                    if kind == "tool":
                        yield ToolCallDelta(0, "call-a", "platform_workspace", "{}")
                        yield Finished("tool_calls")
                    else:
                        yield Delta("Synthetic human reply")
                        yield Finished("stop")

        live = LiveConfig(
            model_id="fixture-model", attempt_period="utc_day", attempt_limit=300,
            activity=ActivityConfig(enabled=True, daily_calls=200),
        )
        assignment = replace(
            resolved(role="activity"), adapter_id=ADAPTER_ID, connection_id="conn-live",
            model_id=live.model_id, endpoint_setting="AI_PLATFORM_LLM_ENDPOINT",
            credential_setting="AI_PLATFORM_LLM_API_KEY",
        )
        provider = PrivateLiveProvider(live, Wire())
        gateway = LLMGateway(lambda _: provider, lambda _: assignment)
        call = request(role="activity", result_model=StepDecision if kind == "planner" else None)
        if kind == "tool":
            call = replace(call, tools=(ToolDefinition(
                name="platform_workspace", parameters_json='{"type":"object"}',
            ),))
        result = await gateway.generate(call)
        assert result.status == "completed" and len(seen) == 1
        if kind == "planner":
            assert result.parsed.action == "complete"
        elif kind == "text":
            assert result.text == "Synthetic human reply"
        else:
            assert len(result.tool_calls) == 1
            assert result.tool_calls[0].name == "platform_workspace"
        assert seen[0].assignment.role_id == "activity"  # same ledger/budget role

    asyncio.run(scenario())
