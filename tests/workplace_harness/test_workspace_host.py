"""Actual OpenCode MCP -> bounded stdio -> production owners; synthetic provider only."""

import asyncio
import json
import os

import httpx
import pytest
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import ManagedFile, Ticket, WorkAttempt
from ai_agent_contracts.api import Permission
from ai_agent_contracts.control import Utterance
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_live import answer
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_opencode import OpenCodeSessions

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_WORKSPACE_IMAGE_ID"), reason="explicit MCP image required"
)
CAPABILITY = "workplace.opencode-session"


class BridgeModel(WorkModel):
    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", body))
        assert "platform_workspace" in {t["function"]["name"] for t in body["tools"]}
        number = len(self.calls)
        if number == 1:
            operation, arguments = (
                "write_note",
                dict(
                    file_id="bridge-note",
                    path="notes/bridge.md",
                    title="Synthetic bridge note",
                    body="Synthetic lighthouse knowledge.",
                    kind="note",
                ),
            )
        elif number == 2:
            assert "bridge-note" in json.dumps(body["messages"][-1])
            operation, arguments = "read_file", dict(file_id="bridge-note")
        elif number == 3:
            assert "Synthetic lighthouse knowledge." in json.dumps(body["messages"][-1])
            operation, arguments = (
                "raise_ticket",
                dict(
                    title="Need synthetic instrument",
                    body="Please consider this instrument.",
                    capability_id="reference.synthetic-missing",
                ),
            )
        elif number == 4:
            assert "ticket_id" in json.dumps(body["messages"][-1])
            return httpx.Response(
                200,
                content=answer("Synthetic note saved; access requested."),
                headers={"Content-Type": "text/event-stream"},
            )
        else:
            raise AssertionError("Unexpected model call")
        call = dict(
            index=0,
            id=f"workspace-{number}",
            type="function",
            function=dict(
                name="platform_workspace",
                arguments=json.dumps(dict(operation=operation, arguments=arguments)),
            ),
        )
        rows = [
            {"choices": [{"delta": {"tool_calls": [call]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            },
        ]
        wire = "".join("data: " + json.dumps(r) + "\n\n" for r in rows) + "data: [DONE]\n\n"
        return httpx.Response(
            200, content=wire.encode(), headers={"Content-Type": "text/event-stream"}
        )


def test_real_workspace_bridge_note_read_ticket_lineage_ack(tmp_path, monkeypatch):
    async def scenario():
        model = BridgeModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        cap = RunnerLimits(20, 512, 128, 45, 65536, 32 * 1024 * 1024)
        template = RunnerTemplate(
            CAPABILITY,
            os.environ["AW_WORKSPACE_IMAGE_ID"],
            PREFIX,
            cap,
            65536,
            max_files=8,
            model_role_id="activity",
            max_model_calls=4,
            max_prompt_chars=100000,
            max_model_tokens=512,
            max_workspace_calls=6,
        )
        async with host.open():
            rt = host.runtime
            fast(rt)
            await install(rt, host.scope, capability=CAPABILITY, areas=["knowledge", "projects"])
            rt.session_harness = None
            controller = OpenCodeSessions(rt, template, output_area="projects")
            controller.install()
            permissions = await rt.store.repository(
                Permission, PRINCIPALS[Owner.WORKSPACE], host.scope
            ).list()
            failures = []
            original = rt._perform

            async def observed(ex):
                try:
                    return await original(ex)
                except Exception as error:
                    error.add_note("Synthetic model calls=" + str(model.count()))
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            made = await open_session(host, "workspace-session")
            identifier = made.json()["resource_id"]
            assert (
                await say(host, "workspace-question", identifier, 1, "Synthetic workspace task")
            ).status_code == 200
            assert model.count() == 0
            await host.start()

            async def read():
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            view = await until(
                read,
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "MCP reply",
                seconds=60,
            )
            assert (
                model.count() == 4
                and view["pending_reply"]["text"] == "Synthetic note saved; access requested."
            )
            [attempt] = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list()
            [note] = await rt.store.repository(
                ManagedFile, PRINCIPALS[Owner.CATALOGUE], host.scope
            ).list()
            assert note.reference in attempt.input_files and note.reference in attempt.result_files
            assert note.dependencies and all(ref in attempt.inputs for ref in note.dependencies)
            [ticket] = await rt.store.repository(
                Ticket, PRINCIPALS[Owner.TICKETS], host.scope
            ).list()
            assert (
                ticket.kind == "capability_request"
                and ticket.request.capability_id == "reference.synthetic-missing"
            )
            assert note.reference in ticket.materials
            assert (
                await rt.store.repository(
                    Permission, PRINCIPALS[Owner.WORKSPACE], host.scope
                ).list()
                == permissions
            )
            draft = view["pending_reply"]
            utterance = await rt.store.repository(
                Utterance, PRINCIPALS[Owner.GATEWAY], host.scope
            ).get(draft["utterance_id"])
            assert note.reference.model_dump(mode="json") in utterance.origin.data["context_files"]
            assert (await ack(host, draft)).status_code == 200
            assert len((await transcript(host, identifier))["entries"]) == 2
            assert not controller.resources and not controller.runners

    asyncio.run(scenario())
