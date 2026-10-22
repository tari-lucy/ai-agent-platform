"""Actual helper stdio bridge has a separate finite owner-call ceiling."""

import asyncio
import os
from dataclasses import replace

import pytest
from ai_agent_application.agent_workplace.ports import RunnerTask
from test_docker import World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HELPER_IMAGE_ID"), reason="explicit image required"
)


def test_helper_rpc_budget_cannot_create_extra_executions(monkeypatch):
    monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_HELPER_IMAGE_ID"])

    async def scenario():
        w = World()
        calls = []

        class Helpers:
            async def call(self, admission, request):
                assert admission == w.admission
                calls.append(request)
                return "{}"

        class Models:
            async def generate(self, *args):
                raise AssertionError("No model calls in helper RPC budget test")

        template = replace(
            w.runner.templates["python-task"], model_role_id="activity", max_helper_calls=2
        )
        w.runner.templates[template.id] = template
        w.runner.helpers, w.runner.models = Helpers(), Models()
        code = """
import os,json,urllib.request
for n in range(3):
    body=json.dumps(dict(call_id=str(n),operation='run_script',arguments={})).encode()
    request=urllib.request.Request(os.environ['AI_WORKPLACE_HELPER_URL'],data=body,
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=2) as response:
        answer=json.load(response)
    assert answer['status']==('completed' if n<2 else 'denied')
    if n==2: assert answer['code']=='RESOURCE_BUSY'
print('HELPER_LIMIT_HELD')
"""
        outcome = await w.runner.run(
            w.admission,
            RunnerTask(
                template.id,
                ("/usr/local/bin/python", "-I", "-c", code),
                (),
                template.ceiling,
                network="gateway",
            ),
        )
        assert outcome.status == "succeeded" and "HELPER_LIMIT_HELD" in outcome.stdout_tail
        assert len(calls) == 2 and w.names[-1][1] == "removed"
        await w.runner.purge()

    asyncio.run(scenario())
