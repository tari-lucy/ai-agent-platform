"""Real OpenCode -> helper MCP -> second Docker container -> managed result -> ACK."""

import asyncio
import json
import os

import httpx
import pytest
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    ManagedFile,
    WorkAttempt,
)
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_live import answer
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS, put
from ai_agent_core.m3_composition.work_session_helpers import PREFIX as HELPER_PREFIX
from ai_agent_core.m3_composition.work_session_opencode import OpenCodeSessions

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_HELPER_IMAGE_ID"), reason="explicit helper image required"
)
CAPABILITY = "workplace.opencode-session"
HELPER = "workplace.python-helper"
SCRIPT = """import os,socket,sys,time
from pathlib import Path
assert os.getuid()!=0
assert not Path('/var/run/docker.sock').exists() and not Path('/host').exists()
assert not any('AI_WORKPLACE' in key or 'SECRET' in key or 'TOKEN' in key for key in os.environ)
assert not os.access('/',os.W_OK)
try:
    socket.create_connection(('1.1.1.1',443),timeout=.2)
except OSError:
    pass
else:
    raise AssertionError('network available')
if sys.argv[1]=='wait':
    time.sleep(30)
Path('outputs/answer.md').write_text('Synthetic helper result '+sys.argv[1])
"""


class HelperModel(WorkModel):
    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", body))
        n = len(self.calls)
        tool = "helpers_scripts"
        if n == 1:
            operation, arguments = "save_script", dict(file_id="synthetic-helper", body=SCRIPT)
        elif n in (2, 4, 6):
            operation, arguments = (
                "run_script",
                dict(
                    file_id="synthetic-helper",
                    version="1",
                    argv=json.dumps(["wait" if n == 6 else "seven"]),
                ),
            )
        elif n == 3:
            assert "REFUSED:ACCESS_DENIED" in json.dumps(body["messages"][-1])
            return httpx.Response(
                200,
                content=answer("Synthetic helper needs explicit permission."),
                headers={"Content-Type": "text/event-stream"},
            )
        elif n == 5:
            # Find the exact owner receipt in the tool response, never infer a host path.
            content = body["messages"][-1]["content"]
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content)
            receipt = json.loads(content)
            assert receipt["status"] == "succeeded" and len(receipt["outputs"]) == 1
            return httpx.Response(
                200,
                content=answer("Synthetic helper output saved."),
                headers={"Content-Type": "text/event-stream"},
            )
        else:
            raise AssertionError("Unexpected model call")
        call = dict(
            index=0,
            id=f"helper-call-{n}",
            type="function",
            function=dict(
                name=tool, arguments=json.dumps(dict(operation=operation, arguments=arguments))
            ),
        )
        chunk = dict(
            id="helper",
            object="chat.completion.chunk",
            model="fixture-model",
            choices=[
                dict(index=0, delta=dict(role="assistant", tool_calls=[call]), finish_reason=None)
            ],
        )
        final = dict(
            id="helper",
            object="chat.completion.chunk",
            model="fixture-model",
            choices=[dict(index=0, delta={}, finish_reason="tool_calls")],
        )
        return httpx.Response(
            200,
            content=(
                "data: "
                + json.dumps(chunk)
                + "\n\ndata: "
                + json.dumps(final)
                + "\n\ndata: [DONE]\n\n"
            ).encode(),
            headers={"Content-Type": "text/event-stream"},
        )


async def helper_setup(rt, scope, *, grant=False):
    for model, identifier in (
        (CapabilityManifest, "session-manifest"),
        (CapabilityBinding, "session-binding"),
        (Permission, "session-permission"),
    ):
        if (model is Permission) != grant:
            continue
        original = await rt.store.repository(model, PRINCIPALS[Owner.WORKSPACE], scope).get(
            identifier
        )
        changes = dict(id="helper-" + identifier, capability_id=HELPER)
        if model is CapabilityBinding:
            changes["binding_id"] = "helper-session-binding"
        if model is Permission:
            changes.update(binding_id="helper-session-binding", operation_type=HELPER)
        item = original.model_copy(update=changes)
        await put(
            rt.store, rt.registry, Owner.WORKSPACE, scope, "seed-" + item.id, item, None, rt.readers
        )


def test_helper_permission_isolation_results_and_stop(tmp_path, monkeypatch):
    async def scenario():
        model = HelperModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        async with host.open():
            rt = host.runtime
            fast(rt)
            await install(rt, host.scope, capability=CAPABILITY, areas=["projects"])
            await helper_setup(rt, host.scope)
            rt.session_harness = None
            image = os.environ["AW_HELPER_IMAGE_ID"]
            outer = RunnerTemplate(
                CAPABILITY,
                image,
                PREFIX,
                RunnerLimits(25, 1024, 128, 55, 65536, 32 * 1024 * 1024),
                65536,
                max_files=8,
                model_role_id="activity",
                max_model_calls=4,
                max_prompt_chars=100000,
                max_helper_calls=4,
                max_workspace_calls=4,
            )
            inner = RunnerTemplate(
                CAPABILITY,
                image,
                HELPER_PREFIX,
                RunnerLimits(3, 128, 16, 10, 16384, 1024 * 1024),
                65536,
                max_files=8,
            )
            controller = OpenCodeSessions(
                rt, outer, output_area="projects", helper_template=inner, helper_capability=HELPER
            )
            controller.install()
            failures = []
            technical = []
            original_factory = rt.session_harness

            async def checked_factory(ex, gateway, gate):
                harness = await original_factory(ex, gateway, gate)
                helpers = harness.runner.helpers
                original_call = helpers.call

                async def checked_call(supplied, request):
                    try:
                        return await original_call(supplied, request)
                    except Exception as error:
                        if model.count("activity") >= 4:
                            failures.append(error)
                        raise

                helpers.call = checked_call
                original_turn = harness.turn

                async def checked_turn(supplied, turn):
                    result = await original_turn(supplied, turn)
                    technical.extend(
                        (e.kind, e.detail if e.kind != "result" else "RESULT") for e in result
                    )
                    return result

                harness.turn = checked_turn
                original_attach = harness.runner._attach

                async def checked_attach(*args):
                    try:
                        return await original_attach(*args)
                    except Exception as error:
                        from ai_agent_adapters.sandbox.docker import cli

                        try:
                            state = await cli("inspect", "--format", "{{json .State}}", args[0])
                            error.add_note("Synthetic container state=" + state.decode())
                        except OSError:
                            pass
                        error.add_note(f"Synthetic model calls={model.count('activity')}")
                        failures.append(error)
                        raise

                harness.runner._attach = checked_attach
                return harness

            rt.session_harness = checked_factory
            perform = rt._perform

            async def observed(ex):
                try:
                    return await perform(ex)
                except Exception as error:
                    error.add_note(f"Synthetic calls={model.count('activity')}; events={technical}")
                    if not (
                        isinstance(error, ContractError)
                        and error.error.code == ErrorCode.STALE_CONTEXT
                    ):
                        failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            opened = await open_session(host, "helper-session")
            identifier = opened.json()["resource_id"]
            assert (
                await say(host, "first", identifier, 1, "Synthetic helper exercise")
            ).status_code == 200
            await host.start()

            async def view():
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            async def draft():
                return await until(
                    view,
                    lambda v: (
                        v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                    ),
                    "helper draft",
                    seconds=70,
                )

            first = await draft()
            assert model.count("activity") == 3
            assert (await ack(host, first["pending_reply"], "first-helper-ack")).status_code == 200
            assert not controller.resources and not controller.helper_runners
            # Trusted fixture grants the separate capability; agent/tool code cannot do this.
            await helper_setup(rt, host.scope, grant=True)
            current = await view()
            assert (
                await say(
                    host,
                    "second",
                    identifier,
                    current["session_version"],
                    "Synthetic operator instruction after granting helper access",
                )
            ).status_code == 200
            second = await draft()
            assert model.count("activity") == 5
            assert (
                await ack(host, second["pending_reply"], "second-helper-ack")
            ).status_code == 200
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list(limit=200)
            completed = next(a for a in attempts if a.helper_runs)
            assert (
                len(completed.helper_runs) == 1 and completed.helper_runs[0].status == "succeeded"
            )
            result_ref = completed.helper_runs[0].outputs[0]
            _, content = await rt.workplace.files.released_content(
                host.scope, result_ref.artifact_id, "api_transfer"
            )
            assert content == b"Synthetic helper result seven"
            result = await rt.store.repository(
                ManagedFile, PRINCIPALS[Owner.CATALOGUE], host.scope
            ).get(result_ref.artifact_id)
            assert result.dependencies and any(
                r.artifact_id == "synthetic-helper" for r in result.derived_from
            )
            assert not controller.resources and not controller.helper_runners
            current = await view()
            assert (
                await say(
                    host,
                    "third",
                    identifier,
                    current["session_version"],
                    "Synthetic interruptible helper exercise",
                )
            ).status_code == 200

            async def helper_running():
                from ai_agent_adapters.sandbox.docker import cli

                if len(controller.resources) != 2:
                    return False
                for runner in controller.helper_runners.values():
                    if runner._active is None:
                        continue
                    try:
                        state = await cli(
                            "inspect", "--format", "{{json .State}}", runner._active[1]
                        )
                    except OSError:
                        return False
                    return json.loads(state)["Running"]
                return False

            await until(helper_running, bool, "running helper container", seconds=60)
            await asyncio.sleep(0.2)  # Script is sleeping; no output has been produced yet.
            execution_task = rt._active
            assert execution_task is not None
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="helper-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert stopped.status_code == 200, stopped.text
            await until(
                lambda: asyncio.sleep(0, result=len(controller.resources)),
                lambda n: n == 0,
                "helper cleanup receipts",
                seconds=30,
            )

            async def execution_finished():
                if execution_task.done():
                    assert execution_task.result().status.value == "CANCELLED"
                    return True
                return False

            await until(execution_finished, bool, "job termination receipt", seconds=30)
            assert not rt.supervisor.snapshot.unconfirmed_resource_ids
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            final = await transcript(host, identifier)
            assert final["pending_reply"] is None and len(final["entries"]) == 5
            assert model.count("activity") == 6

        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            assert model.count("activity") == 6

    asyncio.run(scenario())
