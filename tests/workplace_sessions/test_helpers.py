"""Helper reservations, replay and release over real file/attempt owners; fake executor."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_activity.book import ActivityBook
from ai_agent_application.agent_workplace.ports import (
    RunnerLimits,
    RunnerOutcome,
    RunnerOutput,
    WorkspaceRequest,
)
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from wf_support import ACT, AREA, run

from ai_agent_core.m3_composition import work_session_helpers as module


class Runner:
    def __init__(self):
        self.calls = 0
        self.allowed = True
        self.data = b"Synthetic helper result."
        self.output = RunnerOutput(
            "result.md", hashlib.sha256(self.data).hexdigest(), len(self.data)
        )
        self.status = "succeeded"

    async def authorize(self, *args):
        if not self.allowed:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    async def run(self, gate, task):
        self.calls += 1
        assert task.network == "none" and task.command[:3] == module.PREFIX
        return RunnerOutcome(
            self.status, 0, (self.output,) if self.status == "succeeded" else (), ""
        )

    async def staged(self, gate, output):
        assert output == self.output
        return self.data

    async def cancel(self, gate):
        pass


async def setup(w, monkeypatch):
    source = await w.source()
    gate = await w.admit([source])
    owner = ActivityBook(w.store, w.clock, principal=ACT)
    before = await w.attempt(gate.scope, gate.attempt_id)
    attempt = WorkAttempt.model_validate(
        dict(
            before.model_dump(),
            version=2,
            task_id=None,
            session_id="session",
            session_sequence=1,
            state="submitted",
        )
    )
    await owner._commit("session-attempt", gate.scope, [(attempt, 1)])

    async def verify():
        if not w.active:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    rt = SimpleNamespace(
        workplace=SimpleNamespace(files=w.files),
        session_output_area="knowledge",
        notify=lambda *a: None,
    )
    ex = SimpleNamespace(runtime=rt, verify=verify, extra_sources=())
    monkeypatch.setattr(module, "book", lambda _: owner)
    monkeypatch.setattr(module, "Release", lambda _: w.guard)
    runner = Runner()
    template = RunnerTemplate(
        "files.write",
        "sha256:" + "a" * 64,
        module.PREFIX,
        RunnerLimits(2, 64, 8, 3, 4096, 65536),
        4096,
        max_files=8,
    )
    helpers = module.Helpers(ex, gate, template, runner, {"knowledge"})
    script = json.loads(
        await helpers.call(
            gate,
            WorkspaceRequest(
                "save", "save_script", {"file_id": "script", "body": "print('synthetic')"}
            ),
        )
    )
    request = WorkspaceRequest(
        "run", "run_script", dict(file_id=script["id"], version=str(script["version"]))
    )
    return helpers, gate, owner, runner, request, source


def test_saved_script_output_lineage_and_idempotent_run(tmp_path, monkeypatch):
    async def scenario(w):
        helpers, gate, owner, runner, request, source = await setup(w, monkeypatch)
        result = json.loads(await helpers.call(gate, request))
        assert result["status"] == "succeeded" and runner.calls == 1
        assert json.loads(await helpers.call(gate, request)) == result and runner.calls == 1
        attempt = await owner.attempts_repo(gate.scope).get(gate.attempt_id)
        assert attempt.helper_runs[0].status == "succeeded"
        output, data = await w.files.read(gate, result["outputs"][0]["id"])
        assert data == runner.data and source in output.dependencies
        assert any(r.artifact_id == "script" for r in output.derived_from)
        assert output.reference in attempt.input_files and output.reference in attempt.result_files
        with pytest.raises(ContractError) as changed:
            await helpers.call(
                gate, replace(request, arguments=dict(request.arguments, argv='["new"]'))
            )
        assert changed.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
        assert runner.calls == 1
        await w.ledger.block(AREA, source.source_id, 1, "rights_revoked", w.now)
        with pytest.raises(ContractError):
            await helpers.call(gate, request)
        assert runner.calls == 1

    run(tmp_path, scenario)


@pytest.mark.parametrize("failure", ["permission", "stop", "unknown", "bad_inputs"])
def test_helper_failure_never_becomes_an_uncontrolled_retry(tmp_path, monkeypatch, failure):
    async def scenario(w):
        helpers, gate, owner, runner, request, _ = await setup(w, monkeypatch)
        if failure == "permission":
            runner.allowed = False
        elif failure == "stop":
            w.active = False
        elif failure == "bad_inputs":
            request = replace(
                request, arguments=dict(request.arguments, inputs='[{"id":"other","version":true}]')
            )
        else:
            runner.status = "unknown"
            result = json.loads(await helpers.call(gate, request))
            assert result == {"status": "unknown", "outputs": []}
            with pytest.raises(ContractError):
                await helpers.call(gate, replace(request, call_id="different-id"))
        with pytest.raises(ContractError):
            await helpers.call(gate, request)
        assert runner.calls == (1 if failure == "unknown" else 0)

    run(tmp_path, scenario)
