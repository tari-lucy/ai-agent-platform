"""Real OpenCode + production host owners + synthetic HTTP provider, no paid/network calls."""

import asyncio
import json
import os

import httpx
import pytest
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import ManagedFile, WorkAttempt
from ai_agent_contracts.memory import Episode, Source
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_live import answer
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.activity import tools
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_opencode import OpenCodeSessions

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit verified OpenCode image required"
)

CAPABILITY = "workplace.opencode-session"
RESULT = "Synthetic lighthouse result."


class SessionModel(WorkModel):
    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, request):
        body = json.loads(request.content)
        assert body["model"] == "fixture-model" and body.get("tools")
        self.calls.append(("activity", body))
        number = len(self.calls)
        if number == 1:
            name, arguments = "read", {"filePath": "/work/inputs/manifest.json"}
            assert "Synthetic work question" in json.dumps(body, ensure_ascii=False)
            assert "Опечатка" in json.dumps(body, ensure_ascii=False)
        elif number == 2:
            assert any(m["role"] == "tool" and "[]" in m["content"] for m in body["messages"])
            name, arguments = "write", {"filePath": "/work/outputs/result.md", "content": RESULT}
        elif number == 3:
            assert any(
                m["role"] == "tool" and "success" in m["content"].lower() for m in body["messages"]
            )
            return httpx.Response(
                200,
                content=answer("Synthetic work answer."),
                headers={"Content-Type": "text/event-stream"},
            )
        elif number == 4:
            self.entered.set()
            await self.release.wait()
            return httpx.Response(
                200,
                content=answer("Late result must not be applied."),
                headers={"Content-Type": "text/event-stream"},
            )
        else:
            raise AssertionError("Unscripted model call")
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call-{number}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ]
                    }
                }
            ]
        }
        done = {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
        }
        data = "".join("data: " + json.dumps(row) + "\n\n" for row in (chunk, done, usage))
        return httpx.Response(
            200,
            content=(data + "data: [DONE]\n\n").encode(),
            headers={"Content-Type": "text/event-stream"},
        )


def test_real_opencode_session_saves_via_owner_ack_experience_and_stop(tmp_path, monkeypatch):
    async def scenario():
        model = SessionModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger" / "events.sqlite",
        )
        await enable_workplace_files(host.root)
        cap = RunnerLimits(20, 512, 128, 45, 65536, 32 * 1024 * 1024)
        template = RunnerTemplate(
            CAPABILITY,
            os.environ["AW_OPENCODE_IMAGE_ID"],
            PREFIX,
            cap,
            65536,
            max_files=8,
            model_role_id="activity",
            max_model_calls=4,
            max_prompt_chars=100000,
            max_model_tokens=512,
        )
        async with host.open():
            rt = host.runtime
            fast(rt)
            await install(rt, host.scope, capability=CAPABILITY, areas=["projects", "inbox"])
            rt.session_harness = None  # replace the fixture with real trusted composition
            controller = OpenCodeSessions(rt, template, output_area="projects")
            controller.install()
            assert not any(
                t.available and t.manifest.capability_id == CAPABILITY
                for t in await tools(rt, host.scope)
            )  # no nested persona tool
            failures = []
            outcomes = []
            original_run = DockerTaskRunner.run

            async def observed_run(runner, gate, task):
                outcome = await original_run(runner, gate, task)
                outcomes.append(outcome)
                return outcome

            monkeypatch.setattr(DockerTaskRunner, "run", observed_run)
            original = rt._perform

            async def observed(ex):
                try:
                    return await original(ex)
                except Exception as error:
                    error.add_note(str(outcomes))  # synthetic fixture only
                    error.add_note("Synthetic provider calls=" + str(model.count()))
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            opened = await open_session(host, "real-opencode-session")
            identifier = opened.json()["resource_id"]
            assert (
                await say(host, "real-question", identifier, 1, "Synthetic work question")
            ).status_code == 200
            assert model.count() == 0
            await host.start()

            async def ready():
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            view = await until(
                ready,
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "real draft",
                seconds=60,
            )
            assert view["pending_reply"]["text"] == "Synthetic work answer."
            assert len(view["entries"]) == 1 and view["processed_sequence"] == 0
            assert model.count() == 3
            [attempt] = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list()
            assert attempt.session_id == identifier and len(attempt.result_files) == 1
            [saved] = await rt.store.repository(
                ManagedFile, PRINCIPALS[Owner.CATALOGUE], host.scope
            ).list()
            assert (
                saved.reference == attempt.result_files[0]
                and saved.produced_by_attempt == attempt.id
            )
            assert saved.area == "projects" and saved.provenance == "generated"
            entry, content = await rt.workplace.files.released_content(
                host.scope, saved.id, "api_transfer"
            )
            assert entry == saved and content.decode() == RESULT and saved.dependencies
            assert (await ack(host, view["pending_reply"])).status_code == 200
            view = await transcript(host, identifier)
            assert len(view["entries"]) == 2 and view["entries"][-1]["speaker"] == "character"
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list()
            assert any(e.operation_id == attempt.operation_id for e in episodes)
            assert not controller.runners and not controller.resources
            # The next real container is stopped during its admitted model request.
            assert (
                await say(
                    host,
                    "stop-question",
                    identifier,
                    view["session_version"],
                    "Synthetic stop question",
                )
            ).status_code == 200
            await asyncio.wait_for(model.entered.wait(), 45)
            names = tuple(controller.resources)
            assert names
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                json=dict(
                    type="agent.stop",
                    request_id="stop-real-session",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
                headers=host.env.headers,
            )
            assert stopped.status_code == 200, stopped.text
            model.release.set()
            await until(
                lambda: asyncio.sleep(0, (controller.runners, controller.resources)),
                lambda handles: not any(handles),
                "removed real runner",
                seconds=30,
            )
            for name in names:
                assert not (
                    await cli("ps", "--all", "--filter", "name=" + name, "--format", "{{.ID}}")
                ).strip()
            view = await transcript(host, identifier)
            assert view["pending_reply"] is None and len(view["entries"]) == 3
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list()
            assert not any(s.content == "Late result must not be applied." for s in sources)
            await until(
                lambda: asyncio.sleep(0, rt.supervisor.snapshot.state.value),
                lambda state: state == "STOPPED",
                "job cancellation receipt",
                seconds=20,
            )
        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            assert model.count() == 4

    asyncio.run(scenario())
