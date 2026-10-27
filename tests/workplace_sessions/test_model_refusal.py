"""A live work-turn model call refused before the provider names its exact reason.

Synthetic: the platform's own home/internet environment text and a tool set of the size real
OpenCode 1.19.3 sends with home + internet (8 tools, 15 666 characters, measured on Linux
2026-10-27). No provider, no paid call; limits are never raised by the code under test.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from ai_agent_application.agent_workplace.ports import ModelRequest, WorkAdmission
from ai_agent_application.llm.model import ChatMessage, prompt_size
from ai_agent_application.llm.tools import ToolDefinition

from ai_agent_core.m3_composition.activity import ExecutionModelGateway
from ai_agent_core.m3_composition.service import ModelRefused, PrivateAPIBudget, live_refusal
from ai_agent_core.m3_composition.work_session_opencode import environment_guide

OPENCODE_TOOL_CHARS = 15666  # home + internet, real OpenCode request


def opencode_like_tools() -> tuple[ToolDefinition, ...]:
    schema = json.dumps({"type": "object", "properties": {}})
    names = ("bash", "read", "edit", "write", "glob", "grep", "todowrite", "webfetch")
    each = (OPENCODE_TOOL_CHARS - sum(len(n) + len(schema) for n in names)) // len(names)
    return tuple(
        ToolDefinition(name=n, description="d" * each, parameters_json=schema) for n in names
    )


def turn_request() -> tuple[tuple[ChatMessage, ...], tuple[ToolDefinition, ...]]:
    guide = environment_guide("projects", home=True, internet=True, wake=True)
    context = "Синтетический контекст личности и разговора. " * 80  # ~3.7k, a short turn
    messages = (ChatMessage("system", guide), ChatMessage("user", context))
    return messages, opencode_like_tools()


def budget(limit: int) -> PrivateAPIBudget:
    return PrivateAPIBudget(
        connection_id="conn-live",
        max_attempts=50,
        max_prompt_chars=limit,
        period="utc_day",
        activity_enabled=True,
    )


def test_a_home_internet_turn_outgrows_the_24000_budget_and_says_so():
    messages, tools = turn_request()
    size = prompt_size(messages, tools)
    assert size > 24000  # the reported Windows case: the platform's own text alone
    refused = live_refusal(budget(24000), "conn-live", "activity", "activity", "work", size)
    assert refused == ("MODEL_PROMPT_LIMIT", f"prompt_chars={size} limit=24000")
    # A sufficient configured budget admits the same call; nothing is raised automatically.
    assert live_refusal(budget(40000), "conn-live", "activity", "activity", "work", size) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (dict(budget=None), "MODEL_BUDGET_MISSING"),
        (dict(connection="other"), "MODEL_CONNECTION_MISMATCH"),
        (dict(kind="gm"), "MODEL_ROLE_NOT_ALLOWED"),
        (dict(mode="test"), "MODEL_TEST_MEMORY"),
    ],
)
def test_each_refusal_has_its_own_code(change, reason):
    args = dict(budget=budget(40000), connection="conn-live", kind="activity", mode="work")
    args.update(change)
    found = live_refusal(
        args["budget"], args["connection"], "activity", args["kind"], args["mode"], 100
    )
    assert found is not None and found[0] == reason


def test_the_gateway_keeps_the_reason_the_harness_only_sees_not_admitted():
    scope = SimpleNamespace()
    command = SimpleNamespace(scope=scope, freshness="f", operation_id="op")
    lease = SimpleNamespace(lease_version=1, attempt_id="a-1")

    class Refusing:
        def __init__(self):
            self.command, self.lease, self.cancel = command, lease, asyncio.Event()

        async def generate(self, request, cancel):
            size = prompt_size(request.messages, request.tools)
            raise ModelRefused("MODEL_PROMPT_LIMIT", f"prompt_chars={size} limit=24000")

    gateway = ExecutionModelGateway(Refusing())
    admission = WorkAdmission(scope, "f", "op", "a-1", 1, lambda: True)
    messages, tools = turn_request()
    reply = asyncio.run(
        gateway.generate(admission, ModelRequest("activity", messages, 256, "call-1", tools=tools))
    )
    assert reply.status == "not_admitted" and reply.attempt_id == ""  # no provider attempt
    assert gateway.refused == "MODEL_PROMPT_LIMIT"
