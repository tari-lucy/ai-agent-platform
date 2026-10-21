"""Real Docker probes, opt-in, only synthetic stdin and no host mounts/network."""

import asyncio
import os
from dataclasses import replace

import pytest
from ai_agent_adapters.sandbox.docker import DockerTaskRunner, cli
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask, WorkAdmission
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import Freshness

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_SANDBOX_IMAGE_ID"), reason="explicit local Docker image ID required"
)
CAP = RunnerLimits(3, 128, 16, 3, 8192, 1024 * 1024)


class World:
    def __init__(self):
        from datetime import UTC, datetime

        self.alive = True
        self.revoked = False
        self.names = []
        self.ready = asyncio.Event()
        scope = Scope(
            workspace_id="synthetic",
            persona_id="sample",
            memory_space_id="test",
            memory_mode="test",
            agent_id="agent",
            run_id="run",
        )
        fresh = Freshness(run_id="run", control_epoch=1, profile_version=1, policy_version=1)
        self.admission = WorkAdmission(scope, fresh, "operation", "attempt", 1, lambda: self.alive)
        now = datetime.now(UTC)
        self.record = WorkAttempt(
            id="attempt",
            scope=scope,
            version=1,
            created_at=now,
            updated_at=now,
            task_id="task",
            attempt_no=1,
            operation_id="operation",
            capability_id="python-task",
            capability_version=1,
            freshness=fresh,
            state="submitted",
        )
        template = RunnerTemplate(
            "python-task",
            os.environ["AW_SANDBOX_IMAGE_ID"],
            ("/usr/local/bin/python", "-I"),
            CAP,
            65536,
        )
        self.runner = DockerTaskRunner(
            (template,), self, self, self, authorize=self.authorize, lifecycle=self.lifecycle
        )

    async def attempt(self, scope, attempt_id):
        return self.record

    async def verify(self, *args):
        if self.revoked:
            from ai_agent_contracts.states import ErrorCode

            raise ContractError(ErrorCode.ACCESS_DENIED)

    async def authorize(self, *args):
        pass

    async def lifecycle(self, admission, name, phase):
        self.names.append((name, phase))
        if phase == "register":
            self.ready.set()

    async def read(self, *args):
        raise AssertionError("no inputs requested")

    def task(self, code, cap=CAP):
        return RunnerTask("python-task", ("/usr/local/bin/python", "-I", "-c", code), (), cap)


def test_real_isolation_output_and_cleanup():
    async def scenario():
        w = World()
        code = """
import os, pathlib, socket
assert os.getuid() != 0
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/host').exists()
assert not pathlib.Path('/run/secrets').exists()
assert not os.access('/dev/shm', os.W_OK)
assert not any('KEY' in key or 'TOKEN' in key or 'SECRET' in key for key in os.environ)
try:
    pathlib.Path('/outside-write').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('root writable')
sock = socket.socket()
sock.settimeout(0.2)
assert sock.connect_ex(('1.1.1.1', 443)) != 0
sock.close()
pathlib.Path('outputs/result.txt').write_text('synthetic result')
print('done')
"""
        result = await w.runner.run(w.admission, w.task(code))
        assert result.status == "succeeded", result
        assert result.stdout_tail == "done\n"
        assert await w.runner.staged(w.admission, result.outputs[0]) == b"synthetic result"
        w.revoked = True
        with pytest.raises(ContractError):
            await w.runner.staged(w.admission, result.outputs[0])
        assert [phase for _, phase in w.names] == ["register", "removed"]
        owned = await cli("ps", "--all", "--filter", "name=" + w.names[0][0], "--format", "{{.ID}}")
        assert not owned.strip()
        await w.runner.purge()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["disk", "pids", "output", "timeout", "links", "memory", "cpu"])
def test_real_resource_limits(kind):
    async def scenario():
        w = World()
        code = {
            "disk": (
                "import errno\ntry:\n open('/work/arbitrary','wb').write(b'x'*2000000)\n"
                "except OSError as e:\n assert e.errno == errno.ENOSPC\n print('DISK_BOUNDED')\n"
                "else:\n raise AssertionError('disk unbounded')"
            ),
            "pids": (
                "import subprocess, errno\njobs=[]\ntry:\n for _ in range(100):\n"
                "  jobs.append(subprocess.Popen(['/usr/local/bin/python','-c',"
                "'import time; time.sleep(2)']))\nexcept OSError as e:\n"
                " assert e.errno == errno.EAGAIN\n print('PIDS_BOUNDED')\n"
                "else:\n raise AssertionError('pids unbounded')\n"
                "finally:\n for p in jobs: p.kill()\n for p in jobs: p.wait()"
            ),
            "output": "print('x' * 20000)",
            "timeout": "import time; time.sleep(30)",
            "links": "import os; os.symlink('/etc/passwd', 'outputs/escape')",
            "memory": "data=bytearray(256*1024*1024); print('UNEXPECTED')",
            "cpu": "while True: pass",
        }[kind]
        cap = replace(CAP, cpu_seconds=1) if kind == "cpu" else CAP
        result = await w.runner.run(w.admission, w.task(code, cap))
        expected = {
            "disk": "succeeded",
            "pids": "succeeded",
            "output": "limit",
            "timeout": "timeout",
            "links": "unknown",
            "memory": "failed",
            "cpu": "failed",
        }[kind]
        assert result.status == expected or kind == "memory" and result.status == "limit", result
        assert "UNEXPECTED" not in result.stdout_tail
        if kind in {"disk", "pids"}:
            assert result.stdout_tail.strip() == kind.upper() + "_BOUNDED"
        assert not result.outputs
        assert w.names[-1][1] == "removed"

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["stop", "source"])
def test_revocation_kills_task_and_no_late_result(reason):
    async def scenario():
        w = World()
        running = asyncio.create_task(
            w.runner.run(
                w.admission,
                w.task("import time; time.sleep(2); open('outputs/late.txt','w').write('late')"),
            )
        )
        await w.ready.wait()
        await asyncio.sleep(0.5)
        if reason == "stop":
            w.alive = False
        else:
            w.revoked = True
        result = await running
        assert result.status == "cancelled", result
        assert not result.outputs and not result.stdout_tail
        assert w.names[-1][1] == "removed"

    asyncio.run(scenario())


def test_loopback_broker_uses_bound_gateway_and_denies_unknown_routes():
    from ai_agent_application.agent_workplace.ports import ModelReply

    async def scenario():
        w = World()
        calls = []

        class Models:
            async def generate(self, admission, request):
                assert admission == w.admission and request.role_id == "activity"
                calls.append(request)
                # Shared admission denies the next call. The container never sees credentials.
                return ModelReply(
                    "completed" if len(calls) == 1 else "not_admitted",
                    "synthetic model" if len(calls) == 1 else "",
                    "call",
                )

        template = replace(w.runner.templates["python-task"], model_role_id="activity")
        w.runner.templates[template.id] = template
        w.runner.models = Models()
        code = """
import json, os, urllib.request, urllib.error
base = os.environ['AI_WORKPLACE_MODEL_BASE_URL']
body = json.dumps({'model':'cannot-select-provider', 'messages':[
    {'role':'user','content':'A synthetic prompt'}], 'max_tokens':32}).encode()
def send(path, body):
    return urllib.request.urlopen(urllib.request.Request(base + path,
        data=body, headers={'Content-Type':'application/json'}))
with send('/chat/completions', body) as reply:
    assert json.load(reply)['choices'][0]['message']['content'] == 'synthetic model'
try:
    send('/chat/completions', body)
except urllib.error.HTTPError as e:
    assert e.code == 429
else:
    raise AssertionError('budget bypass')
try:
    send('/internal/commands', b'{}')
except urllib.error.HTTPError as e:
    assert e.code == 404
else:
    raise AssertionError('core route exposed')
print('BROKER_BOUNDED')
"""
        result = await w.runner.run(w.admission, replace(w.task(code), network="gateway"))
        assert result.status == "succeeded", result
        assert result.stdout_tail.strip() == "BROKER_BOUNDED"
        assert len(calls) == 2
        assert all(request.max_output_tokens == 32 for request in calls)
        assert [request.call_id for request in calls] == ["sandbox-1", "sandbox-2"]

    asyncio.run(scenario())


def test_real_catalogue_input_result_provenance_and_revocation(tmp_path):
    """Reuse the AW-02 real owner fixture, not another filesystem or fake source guard."""
    import runpy
    from pathlib import Path

    from ai_agent_contracts.api import Receipt

    support = runpy.run_path(str(Path(__file__).parents[1] / "workplace_files" / "wf_support.py"))

    async def scenario():
        w = await support["World"].create(tmp_path)
        try:
            source = await w.source()
            admission = await w.admit([source])
            change, data = w.change()
            first = await w.files.write(admission, change, data)
            admission = await w.admit(files=[first.reference])
            # Trusted activity owner records a separate, permitted runner attempt.
            attempt = await w.attempt(admission.scope, admission.attempt_id)
            command = w.command(admission.scope, "bind-synthetic-runner")
            async with w.store.transaction(support["ACT"], admission.scope, command) as unit:
                await unit.put(
                    attempt.model_copy(update={"version": 2, "capability_id": "python-task"}),
                    expected_version=1,
                )
                await unit.complete(
                    Receipt(
                        request_id=command.request_id,
                        scope=command.scope,
                        status="completed",
                        resource_id=attempt.id,
                        version=2,
                    )
                )
            technical = World()

            async def authorize(bound, capability):
                assert bound == admission and capability == "python-task" and w.allowed

            runner = DockerTaskRunner(
                tuple(technical.runner.templates.values()),
                w.files,
                w,
                w.guard,
                authorize=authorize,
                lifecycle=technical.lifecycle,
            )
            task = replace(
                technical.task(
                    "from pathlib import Path; text=Path('inputs/input-0000').read_bytes(); "
                    "Path('outputs/result.txt').write_bytes(text + b' Checked.')"
                ),
                inputs=(first.reference,),
            )
            result = await runner.run(admission, task)
            assert result.status == "succeeded", result
            output = await runner.staged(admission, result.outputs[0])
            change, output = w.change(output, file_id="result", request="save-result")
            saved = await w.files.write(admission, change, output)
            assert saved.dependencies == [source] and saved.derived_from == [first.reference]
            await w.ledger.block(support["AREA"], source.source_id, 1, "operator_delete", w.now)
            with pytest.raises(ContractError):
                await runner.staged(admission, result.outputs[0])
            with pytest.raises(ContractError):
                await w.files.read(admission, saved.id)
            await runner.purge()
        finally:
            await w.close()

    asyncio.run(scenario())


def test_purge_waits_for_cleanup_and_fences_late_cache(monkeypatch):
    async def scenario():
        w = World()
        reached, release = asyncio.Event(), asyncio.Event()
        verify = w.runner._verify

        async def pause_before_release(*args):
            await verify(*args)
            if w.names and w.names[-1][1] == "removed":
                reached.set()
                await release.wait()

        monkeypatch.setattr(w.runner, "_verify", pause_before_release)
        task = w.task("open('outputs/late.txt','w').write('synthetic')")
        running = asyncio.create_task(w.runner.run(w.admission, task))
        await asyncio.wait_for(reached.wait(), 10)
        with pytest.raises(ContractError):
            await w.runner.run(w.admission, task)
        purging = asyncio.create_task(w.runner.purge())
        await asyncio.sleep(0)
        assert not purging.done()
        release.set()
        assert (await running).status == "cancelled"
        await purging
        assert w.runner._cache is None

    asyncio.run(scenario())


def test_external_watchdog_stops_unresponsive_entry(monkeypatch):
    import ai_agent_adapters.sandbox.docker as docker

    async def scenario():
        w = World()
        monkeypatch.setattr(docker, "ENTRY", "import time; time.sleep(30)")
        result = await w.runner.run(w.admission, w.task("print('never executed')"))
        assert result.status == "timeout" and not result.outputs
        assert w.names[-1][1] == "removed"

    asyncio.run(scenario())
