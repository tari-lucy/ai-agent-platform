"""Ordinary launcher -> real OpenCode/MCP -> networkless Python -> archive report + STOP."""

import asyncio
import io
import json
import os
import zipfile

import httpx
import pytest
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import TicketDraft
from ai_agent_contracts.agent_workplace import CapabilityRequest, WorkAttempt
from session_support import ack
from test_activity import WorkModel, fast, prepared, until
from test_library_host import decide
from test_session_launcher import stop
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HELPER_IMAGE_ID"), reason="explicit helper image required"
)
AREAS = ["projects", "inbox"]
SCRIPT = """import json,os,socket,sys,time,zipfile
from pathlib import Path
assert os.getuid()!=0 and not os.access('/',os.W_OK)
assert not Path('/var/run/docker.sock').exists() and not Path('/host').exists()
assert not any('AI_WORKPLACE' in k or 'SECRET' in k or 'TOKEN' in k for k in os.environ)
try:
    socket.create_connection(('1.1.1.1',443),timeout=.1)
except OSError:
    pass
else:
    raise AssertionError('network available')
if len(sys.argv)>1:
    time.sleep(40)
with zipfile.ZipFile('inputs/input-0001') as z:
    assert len(z.infolist())<100 and z.getinfo('fabric.mod.json').file_size<65536
    manifest=json.loads(z.read('fabric.mod.json'))
Path('outputs/report.json').write_text(json.dumps({'mod_id':manifest['id'],'entries':len(z.infolist())}))
"""


class ArchiveModel(WorkModel):
    archive_id = ""

    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", body))
        n = len(self.calls)
        assert "PRIVATE_BINARY_PAYLOAD_9347" not in json.dumps(body)
        if n in (1, 5):
            op, args = (
                "save_script",
                dict(file_id="archive-script" + ("-slow" if n == 5 else ""), body=SCRIPT),
            )
            name = "helpers_scripts"
        elif n in (2, 6):
            op, args = (
                "run_script",
                dict(
                    file_id="archive-script" + ("-slow" if n == 6 else ""),
                    version="1",
                    inputs=json.dumps([dict(id=self.archive_id, version=1)]),
                    argv=json.dumps(["wait"] if n == 6 else []),
                ),
            )
            name = "helpers_scripts"
        elif n == 3:
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content)
            receipt = json.loads(content)
            assert receipt["status"] == "succeeded" and receipt["outputs"]
            op, args = "read_file", dict(file_id=receipt["outputs"][0]["id"])
            name = "platform_workspace"
        elif n == 4:
            assert "synthetic_mod" in json.dumps(body["messages"][-1])
            return self.response(
                body, dict(role="assistant", content="Synthetic archive report saved."), "stop"
            )
        else:
            raise AssertionError("unscripted model call")
        return self.response(
            body,
            dict(
                role="assistant",
                content=None,
                tool_calls=[
                    dict(
                        id="archive-call-" + str(n),
                        type="function",
                        function=dict(
                            name=name, arguments=json.dumps(dict(operation=op, arguments=args))
                        ),
                    )
                ],
            ),
            "tool_calls",
        )

    @staticmethod
    def response(body, message, reason):
        if not body.get("stream"):
            return httpx.Response(
                200,
                json=dict(
                    id="synthetic",
                    model="fixture-model",
                    choices=[dict(index=0, message=message, finish_reason=reason)],
                ),
            )
        if message.get("tool_calls"):
            message = dict(
                message, tool_calls=[dict(c, index=i) for i, c in enumerate(message["tool_calls"])]
            )
        first = dict(
            id="synthetic",
            model="fixture-model",
            choices=[dict(index=0, delta=message, finish_reason=None)],
        )
        last = dict(
            id="synthetic",
            model="fixture-model",
            choices=[dict(index=0, delta={}, finish_reason=reason)],
        )
        return httpx.Response(
            200,
            content=(
                "data: "
                + json.dumps(first)
                + "\n\ndata: "
                + json.dumps(last)
                + "\n\ndata: [DONE]\n\n"
            ).encode(),
            headers={"Content-Type": "text/event-stream"},
        )


async def grant(host, capability, key):
    rt = host.runtime
    ticket = await rt.tickets.raise_ticket(
        key,
        host.scope,
        "agent",
        "workplace-agent",
        TicketDraft(
            kind="capability_request",
            title="Synthetic permission",
            summary="Synthetic archive exercise",
            attempted="Not run",
            decision_needed="Grant once",
            basis=capability,
            request=CapabilityRequest(
                capability_id=capability, areas=AREAS, constraints="Synthetic"
            ),
        ),
        "Synthetic",
    )
    await decide(
        host,
        key + "-decision",
        dict(
            action="grant",
            capability_id=capability,
            binding_id="opencode." + capability,
            ticket_id=ticket.resource_id,
            ticket_version=1,
            level="automatic",
            duration_seconds=86400,
            areas=AREAS,
            constraints="Synthetic",
        ),
        1,
    )


def test_launcher_reads_binary_archive_and_stop_removes_both_containers(tmp_path, monkeypatch):
    async def scenario():
        model = ArchiveModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root, max_file_bytes=104857600)
        await enable_work_sessions(
            host.root,
            image_id=os.environ["AW_HELPER_IMAGE_ID"],
            areas=AREAS,
            output_area="projects",
            ceiling_days=7,
            helper_enabled=True,
            helper_wall_seconds=60,
            helper_cpu_seconds=5,
            helper_memory_mb=256,
            helper_disk_mb=64,
            helper_max_input_bytes=16777216,
            max_model_calls=8,
            helper_max_calls=8,
            wall_seconds=120,
        )
        async with host.open():
            rt = host.runtime
            fast(rt)
            factory = rt.session_harness
            controller = factory.__self__
            failures = []

            async def observed(ex, models, gate):
                harness = await factory(ex, models, gate)
                original_call = harness.runner.helpers.call

                async def checked(*args):
                    try:
                        return await original_call(*args)
                    except Exception as error:
                        failures.append(error)
                        raise

                harness.runner.helpers.call = checked
                return harness

            rt.session_harness = observed
            assert (
                rt.session_unavailable is None
                and rt.session_helper_capability == "workplace.scripts"
            )
            await grant(host, "workplace.opencode-session", "parent-right")
            await grant(host, "workplace.scripts", "helper-right")
            raw = io.BytesIO()
            with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as z:
                z.writestr("fabric.mod.json", json.dumps(dict(id="synthetic_mod", version="1.0")))
                z.writestr(
                    "payload.class", b"PRIVATE_BINARY_PAYLOAD_9347" + b"x" * (6 * 1024 * 1024)
                )
            data = raw.getvalue()
            scope = {
                key: value
                for key, value in host.scope.model_dump(mode="json").items()
                if key in {"workspace_id", "persona_id", "memory_space_id", "memory_mode"}
            }
            response = await host.env.client.post(
                "/api/v1/workplace-files/upload",
                params={
                    **scope,
                    "area": "inbox",
                    "name": "synthetic-mod.jar",
                    "media_type": "application/java-archive",
                },
                headers={**host.env.headers, "Content-Type": "application/octet-stream"},
                content=data,
            )
            assert response.status_code == 200, response.text
            model.archive_id = response.json()["id"]
            session = (await open_session(host, "archive-launcher")).json()["resource_id"]
            assert (
                await say(
                    host,
                    "archive-input",
                    session,
                    1,
                    "Inspect the synthetic archive, without running its code.",
                )
            ).status_code == 200
            await host.start()

            async def view():
                if failures:
                    raise failures[0]
                value = await transcript(host, session)
                progress = value.get("progress")
                if progress and progress["state"] in ("failed", "unknown"):
                    raise AssertionError(
                        "Synthetic turn failed: " + str(progress.get("error_code"))
                    )
                return value

            ready = await asyncio.wait_for(
                until(
                    view,
                    lambda v: v.get("pending_reply") and v["pending_reply"]["acknowledgeable"],
                    "archive reply",
                    seconds=100,
                ),
                timeout=150,
            )
            assert ready["pending_reply"]["text"] == "Synthetic archive report saved."
            assert (await ack(host, ready["pending_reply"], "archive-ack")).status_code == 200
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list(limit=200)
            completed = next(a for a in attempts if a.helper_runs)
            ref = completed.helper_runs[0].outputs[0]
            entry, body = await rt.workplace.files.released_content(
                host.scope, ref.artifact_id, "api_transfer"
            )
            assert json.loads(body)["mod_id"] == "synthetic_mod"
            assert any(f.artifact_id == model.archive_id for f in entry.derived_from)
            assert not controller.resources and model.count("activity") == 4
            current = await view()
            assert (
                await say(
                    host,
                    "archive-stop-input",
                    session,
                    current["session_version"],
                    "Synthetic interruptible helper",
                )
            ).status_code == 200

            async def running():
                from ai_agent_adapters.sandbox.docker import cli

                for runner in controller.helper_runners.values():
                    if runner._active is not None:
                        try:
                            state = await cli(
                                "inspect", "--format", "{{json .State}}", runner._active[1]
                            )
                        except OSError:
                            continue  # Completed runners retain an id whose container was removed.
                        if json.loads(state)["Running"]:
                            return len(controller.resources) == 2
                return False

            await asyncio.wait_for(
                until(running, bool, "active helper container", seconds=60), timeout=90
            )
            active = rt._active
            await stop(host, "stop-archive-helper")
            await until(
                lambda: asyncio.sleep(0, result=len(controller.resources)),
                lambda n: n == 0,
                "both removal receipts",
                seconds=30,
            )
            assert (
                active is None or (await asyncio.wait_for(active, 30)).status.value == "CANCELLED"
            )
            await asyncio.wait_for(
                until(
                    lambda: asyncio.sleep(0, result=rt.supervisor.snapshot),
                    lambda run: run.state.value == "STOPPED" and not run.unconfirmed_resource_ids,
                    "job and container removal receipts",
                    seconds=30,
                ),
                timeout=30,
            )
            assert not rt.supervisor.snapshot.unconfirmed_resource_ids
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert (await view())["pending_reply"] is None and model.count("activity") == 6
        async with host.open():
            assert (
                host.runtime.supervisor.snapshot.state.value == "STOPPED"
                and model.count("activity") == 6
            )

    asyncio.run(scenario())
