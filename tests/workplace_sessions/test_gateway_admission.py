"""A miswired session admission must not use another job's model/budget gateway."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from ai_agent_application.agent_workplace.ports import ModelRequest, WorkAdmission
from ai_agent_application.llm.model import ChatMessage
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import Freshness

from ai_agent_core.m3_composition.activity import ExecutionModelGateway


def test_gateway_binds_scope_epoch_operation_lease_and_forwards_the_token_ceiling():
    async def scenario():
        scope = Scope(
            workspace_id="sample",
            persona_id="fictional",
            memory_space_id="work",
            memory_mode="work",
            agent_id="agent",
            run_id="run",
        )
        fresh = Freshness(run_id="run", control_epoch=1, profile_version=1, policy_version=1)
        sent = []

        async def generate(request, cancel):
            sent.append(request)
            return SimpleNamespace(
                attempts=[], failure=None, status="completed", text="done", parsed=None
            )

        execution = SimpleNamespace(
            command=SimpleNamespace(scope=scope, freshness=fresh, operation_id="operation"),
            lease=SimpleNamespace(lease_version=1),
            cancel=asyncio.Event(),
            generate=generate,
        )
        gateway = ExecutionModelGateway(execution)
        admission = WorkAdmission(scope, fresh, "operation", "work-attempt", 1, lambda: True)
        request = ModelRequest("activity", (ChatMessage("user", "Synthetic"),), 32, call_id="one")
        for wrong in (
            replace(admission, scope=scope.model_copy(update={"memory_space_id": "foreign"})),
            replace(admission, freshness=fresh.model_copy(update={"control_epoch": 2})),
            replace(admission, operation_id="other"),
            replace(admission, lease_version=2),
            replace(admission, is_current=lambda: False),
        ):
            assert (await gateway.generate(wrong, request)).status == "not_admitted"
        assert sent == []
        assert (await gateway.generate(admission, request)).status == "completed"
        assert sent[0].max_output_tokens == 32 and sent[0].operation_id == "act-one"

        async def denied():
            return False

        gateway.release = denied
        assert (await gateway.generate(admission, request)).status == "not_admitted"
        assert len(sent) == 1
        decisions = iter((True, False))

        async def revoked_during_model():
            return next(decisions)

        gateway.release = revoked_during_model
        late = await gateway.generate(admission, request)
        assert late.status == "not_admitted" and late.text == "" and len(sent) == 2

    asyncio.run(scenario())
