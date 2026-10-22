"""Staged worker bytes cannot bypass the existing file owner or publish a false result."""

import hashlib
from types import SimpleNamespace

import pytest
from ai_agent_application.agent_workplace.ports import RunnerOutput
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from wf_support import AREA, run

from ai_agent_core.m3_composition.work_session_outputs import save_outputs


def test_same_output_name_in_two_attempts_keeps_both_results(tmp_path):
    async def scenario(w):
        source = await w.source()
        data = b"Synthetic result."
        output = RunnerOutput("result.md", hashlib.sha256(data).hexdigest(), len(data))

        class Harness:
            async def staged(self, gate, selected):
                return data

        async def verify():
            pass

        ex = SimpleNamespace(
            runtime=SimpleNamespace(
                workplace=SimpleNamespace(files=w.files), session_output_area="knowledge"
            ),
            verify=verify,
        )
        refs = []
        for _ in range(2):
            admission = await w.admit([source])
            attempt = await w.attempt(admission.scope, admission.attempt_id)
            subject = attempt.model_copy(
                update={"task_id": None, "session_id": "session", "session_sequence": 1}
            )
            refs += await save_outputs(ex, Harness(), admission, subject, (output,))
        assert refs[0].artifact_id != refs[1].artifact_id
        entries = await w.files.list(AREA, "knowledge")
        assert len(entries) == 2 and entries[0].path != entries[1].path
        assert all(entry.dependencies == [source] for entry in entries)

    run(tmp_path, scenario)


@pytest.mark.parametrize("failure", ["digest", "path", "revoked", "stop"])
def test_refused_output_is_not_published(tmp_path, failure):
    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        attempt = await w.attempt(admission.scope, admission.attempt_id)
        subject = attempt.model_copy(
            update={"task_id": None, "session_id": "session", "session_sequence": 1}
        )
        data = b"Synthetic staged result."
        output = RunnerOutput(
            "../outside.md" if failure == "path" else "result.md",
            "0" * 64 if failure == "digest" else hashlib.sha256(data).hexdigest(),
            len(data),
        )
        reads = []

        class Harness:
            async def staged(self, gate, selected):
                reads.append(selected)
                if failure == "revoked":
                    await w.ledger.block(AREA, source.source_id, 1, "rights_revoked", w.now)
                if failure == "stop":
                    w.active = False
                return data

        async def verify():
            if not w.active:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        ex = SimpleNamespace(
            runtime=SimpleNamespace(
                workplace=SimpleNamespace(files=w.files), session_output_area="knowledge"
            ),
            verify=verify,
        )
        with pytest.raises(ContractError):
            await save_outputs(ex, Harness(), admission, subject, (output,))
        assert await w.files.list(AREA, "knowledge") == []
        if failure == "path":
            assert not reads

    run(tmp_path, scenario)
