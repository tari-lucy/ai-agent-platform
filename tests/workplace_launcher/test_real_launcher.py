"""Ordinary serve with the offline configuration and real OpenCode/Docker (Linux or Windows).

Real init/build, `enable-work-sessions` with the real readiness check of the pinned image, the
technical access ticket, the operator's decision, real OpenCode through the shared gateway with
a synthetic HTTP provider, ACK, STOP, restart STOPPED. No programmatic install, no network,
no paid calls.
"""

import asyncio
import json
import os

import httpx
import pytest
from session_support import ack
from test_activity import WorkModel, fast, prepared, until
from test_live import answer
from test_session_launcher import AREAS, grant, stop, tickets
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit verified OpenCode image required"
)
REPLY = "Синтетический ответ из OpenCode."


class OpenCodeModel(WorkModel):
    async def __call__(self, request):
        body = json.loads(request.content)
        assert body["model"] == "fixture-model" and body.get("tools")
        self.calls.append(("activity", body))
        assert len(self.calls) <= 2, "unscripted model call"
        return httpx.Response(
            200, content=answer(REPLY), headers={"Content-Type": "text/event-stream"}
        )


def test_operator_message_is_answered_by_real_opencode_after_access_decision(tmp_path, monkeypatch):
    async def scenario():
        model = OpenCodeModel()
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
            rt, scope = host.runtime, host.scope
            fast(rt)
            assert rt.session_unavailable is None and rt.session_harness is not None
            session = (await open_session(host, "open", "Разговор")).json()["resource_id"]
            assert (await say(host, "say", session, 1, "Синтетический вопрос.")).status_code == 200
            await host.start()
            view = await until(
                lambda: transcript(host, session),
                lambda v: v["waiting_reason"] == "access_requested",
                "access ticket",
                seconds=30,
            )
            assert model.count() == 0
            await grant(host, await rt.tickets.get(scope, view["access_ticket_id"]))
            view = await until(
                lambda: transcript(host, session),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "real OpenCode draft",
                seconds=180,
            )
            assert view["pending_reply"]["text"] == REPLY
            assert (await ack(host, view["pending_reply"], "real-ack")).status_code == 200
            view = await transcript(host, session)
            assert [e["speaker"] for e in view["entries"]] == ["operator", "character"]
            calls = model.count()
            assert calls >= 1 and len(await tickets(rt, scope)) == 1
            await stop(host, "real-stop")
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await asyncio.sleep(1.0)
            assert model.count() == calls

    asyncio.run(scenario())
