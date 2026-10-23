"""The OpenCode environment is explained as adapter facts, without changing task or admission."""

import asyncio

from ai_agent_application.agent_workplace.ports import ModelReply, ModelRequest
from ai_agent_application.llm import ChatMessage, ToolDefinition

from ai_agent_core.m3_composition.work_session_opencode import EnvironmentModels


def test_guide_uses_configured_output_area_and_preserves_request():
    async def scenario():
        seen = []

        class Delegate:
            async def generate(self, admission, request):
                seen.append((admission, request))
                return ModelReply("completed", "synthetic", "receipt")

        gateway = EnvironmentModels(Delegate(), "inbox")
        original = ModelRequest(
            "activity",
            (ChatMessage("system", "Synthetic rules"), ChatMessage("user", "Synthetic task")),
            256,
            call_id="call",
            tools=(ToolDefinition(name="declared", parameters_json='{"type":"object"}'),),
        )
        admission = object()
        response = await gateway.generate(admission, original)
        assert response.status == "completed" and len(seen) == 1
        forwarded = seen[0][1]
        assert seen[0][0] is admission and forwarded.call_id == original.call_id
        assert forwarded.tools == original.tools and forwarded.messages[1:] == original.messages[1:]
        assert original.messages[0].content == "Synthetic rules"
        assert "/work/outputs/" in forwarded.messages[0].content
        assert "область inbox" in forwarded.messages[0].content
        invalid = ModelRequest(
            "activity", (ChatMessage("user", "No system"),), 32, call_id="invalid"
        )
        assert (await gateway.generate(admission, invalid)).status == "not_admitted"
        assert len(seen) == 1

    asyncio.run(scenario())
