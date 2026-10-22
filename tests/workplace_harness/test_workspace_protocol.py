"""Untrusted workspace frames are closed; Cyrillic owner responses remain bounded."""

import io
import json

import pytest
from ai_agent_adapters.agent_harness.workspace_mcp import respond
from ai_agent_adapters.sandbox.model_protocol import workspace_request


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "forged"},
        {"principal": "operator"},
        {"operation": "shell"},
        {"arguments": {"body": True}},
        {"arguments": {"body": "x" * 4001}},
    ],
)
def test_workspace_frames_cannot_choose_scope_identity_or_unregistered_io(change):
    row = dict(kind="workspace", call_id="one", operation="capabilities", arguments={})
    with pytest.raises(ValueError):
        workspace_request(dict(row, **change))


def test_mcp_preserves_a_bounded_cyrillic_reply_without_network(monkeypatch):
    text = "я" * 4000
    wire = json.dumps({"status": "completed", "text": text}).encode()
    assert len(wire) > 20000  # escaped JSON is larger than the owner text itself
    monkeypatch.setenv("AI_WORKPLACE_WORKSPACE_URL", "http://127.0.0.1:1/v1/workspace")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: io.BytesIO(wire))
    reply = respond(
        {
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "workspace",
                "arguments": {"operation": "capabilities", "arguments": {}},
            },
        }
    )
    assert reply == {"content": [{"type": "text", "text": text}], "isError": False}
