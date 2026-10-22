"""Helper transport cannot choose host authority or a shell command."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from ai_agent_adapters.agent_harness import helper_mcp
from ai_agent_adapters.agent_harness.helper_mcp import respond
from ai_agent_adapters.agent_harness.worker import configuration
from ai_agent_adapters.sandbox.helper_protocol import helper_request
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "forged"},
        {"command": "shell"},
        {"operation": "shell"},
        {"arguments": {"body": True}},
        {"arguments": {"body": "x" * 4001}},
        {"call_id": ""},
    ],
)
def test_closed_helper_frames(change):
    with pytest.raises(ValueError):
        helper_request(
            dict(
                dict(kind="helper", call_id="one", operation="save_script", arguments={}), **change
            )
        )


def test_fixed_helper_mcp_is_disabled_by_default_and_adds_no_shell_permission():
    assert "helpers" not in configuration("http://127.0.0.1:1/v1", 64)["mcp"]
    config = configuration("http://127.0.0.1:1/v1", 64, helpers=True)
    assert config["mcp"]["helpers"]["command"] == [
        "/usr/local/bin/python",
        "-I",
        "/opt/agent-harness/helper_mcp.py",
    ]
    assert config["agent"]["platform"]["permission"]["*"] == "deny"
    assert config["agent"]["platform"]["permission"]["helpers_*"] == "allow"
    schema = respond({"method": "tools/list"})["tools"][0]["inputSchema"]
    assert schema["properties"]["operation"]["enum"] == ["save_script", "run_script"]


def test_mcp_error_preserves_request_id_so_client_does_not_wait_for_a_timeout(monkeypatch, capsys):
    request = dict(id=7, method="tools/call", params={"name": "not-registered"})
    monkeypatch.setattr(
        helper_mcp.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO((json.dumps(request) + "\n").encode())),
    )
    helper_mcp.main()
    answer = json.loads(capsys.readouterr().out)
    assert answer["id"] == 7 and answer["error"]["message"] == "HELPER_REQUEST_REFUSED"


def test_new_input_cancels_a_queued_old_sequence_before_context_or_model(monkeypatch):
    from ai_agent_core.m3_composition import work_session_runtime, work_sessions

    async def get(*args):
        return SimpleNamespace(state="open", pending_reply=None, generation=1, last_sequence=3)

    monkeypatch.setattr(work_sessions, "sessions", lambda _: SimpleNamespace(get=get))
    request = SimpleNamespace(
        work_session_id="session", work_session_generation=1, work_session_sequence=1
    )
    ex = SimpleNamespace(
        runtime=SimpleNamespace(session_harness=object(), session_capability="cap"),
        command=SimpleNamespace(scope=object()),
        args=SimpleNamespace(request=request),
    )
    with pytest.raises(ContractError) as caught:
        asyncio.run(work_session_runtime.perform(ex))
    assert caught.value.error.code == ErrorCode.STALE_CONTEXT
