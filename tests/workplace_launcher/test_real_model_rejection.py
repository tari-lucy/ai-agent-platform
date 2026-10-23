"""Real OpenCode must stop on a host-rejected result, not back off as if rate limited."""

import asyncio
import json
import os

import httpx
import pytest
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkAttempt
from test_activity import WorkModel, fast, prepared, until
from test_session_launcher import AREAS, grant
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit verified OpenCode image required"
)


class RejectedModel(WorkModel):
    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", body))
        assert len(self.calls) == 1, "a rejected model result was retried"
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "synthetic",
                                    "type": "function",
                                    "function": {
                                        "name": "undeclared_instrument",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
        ]
        wire = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=wire.encode(), headers={"Content-Type": "text/event-stream"}
        )


def test_real_opencode_ends_after_one_rejected_model_call(tmp_path, monkeypatch, caplog):
    async def scenario():
        model = RejectedModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        await enable_work_sessions(
            host.root,
            image_id=os.environ["AW_OPENCODE_IMAGE_ID"],
            areas=AREAS,
            output_area="projects",
            ceiling_days=7,
        )
        async with host.open():
            rt = host.runtime
            fast(rt)
            session = (await open_session(host, "open")).json()["resource_id"]
            await say(host, "say", session, 1, "Use a declared instrument for this synthetic task.")
            await host.start()
            view = await until(
                lambda: transcript(host, session),
                lambda v: v["waiting_reason"] == "access_requested",
                "access request",
                seconds=30,
            )
            await grant(host, await rt.tickets.get(host.scope, view["access_ticket_id"]))

            async def terminal():
                rows = await rt.store.repository(
                    WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
                ).list(limit=20)
                return next(
                    (r for r in rows if r.session_id == session and r.state == "failed"), None
                )

            await until(terminal, lambda v: v is not None, "terminal rejected call", seconds=45)
            assert model.count() == 1
            assert (await transcript(host, session))["pending_reply"] is None
            assert "TOOL_UNDECLARED_NAME" in caplog.text

    asyncio.run(scenario())
