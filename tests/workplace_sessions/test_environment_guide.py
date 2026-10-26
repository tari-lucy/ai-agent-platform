"""The OpenCode environment is explained as adapter facts, without changing task or admission."""

import asyncio

from ai_agent_application.agent_workplace.ports import ModelReply, ModelRequest
from ai_agent_application.llm import ChatMessage, ToolDefinition

from ai_agent_core.m3_composition.work_session_opencode import EnvironmentModels, environment_guide


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


def test_disabled_helpers_are_not_advertised_as_available():
    guide = environment_guide("projects")
    assert "Python helper в этом запуске не подключён" in guide
    assert "save_script сохраняет" not in guide
    assert "не компьютер оператора" in guide
    assert "согласования каждого подшага" in guide


def test_enabled_helper_facts_follow_the_configured_template():
    from ai_agent_core.local_host.layout import WorkSessionsConfig
    from ai_agent_core.local_host.work_sessions import helper_template

    config = WorkSessionsConfig(
        image_id="sha256:" + "a" * 64,
        areas=("inbox", "projects"),
        output_area="projects",
        helper_enabled=True,
        helper_capability_id="example.archive-python",
        helper_wall_seconds=45,
        helper_cpu_seconds=10,
        helper_memory_mb=512,
        helper_max_input_bytes=8388608,
        helper_output_bytes=8192,
    )
    guide = environment_guide("projects", helper_template(config), config.helper_capability_id)
    assert "example.archive-python" in guide and "это ещё не Permission" in guide
    assert "wall 45 с, CPU 10 с, RAM 512 МиБ, вход 8388608 байт, выход 8192 байт" in guide
    assert "localhost/127.0.0.1" in guide and "без сети" in guide
    assert "не меняет сетевой запрет" in guide
    assert "save_script сохраняет" in guide and "Только receipt run_script" in guide
    assert "а не чтение его байтов" in guide
    assert "не запускай код архива" in guide
