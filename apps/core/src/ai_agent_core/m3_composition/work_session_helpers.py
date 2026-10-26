"""Managed Python helpers, separate networkless containers, same admitted activity attempt."""

import hashlib
import json
from typing import TYPE_CHECKING, cast

from ai_agent_adapters.sandbox.docker import DockerTaskRunner
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_application.agent_workplace.ports import (
    AgentHarness,
    RunnerOutput,
    RunnerTask,
    WorkAdmission,
    WorkspaceRequest,
)
from ai_agent_application.memory import derived_id
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import FileWrite, HelperRun
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .activity import book
from .helper_publication import publication_check
from .work_session_outputs import save_outputs
from .work_session_runtime import Release

if TYPE_CHECKING:
    from .service import Execution

PREFIX = ("/usr/local/bin/python", "-I", "/work/inputs/input-0000")


class Helpers:
    def __init__(
        self,
        ex: Execution,
        gate: WorkAdmission,
        template: RunnerTemplate,
        runner: DockerTaskRunner,
        required_areas: set[str],
    ) -> None:
        self.ex, self.gate, self.template, self.runner = ex, gate, template, runner
        self.required_areas = required_areas

    async def call(self, admission: WorkAdmission, request: WorkspaceRequest) -> str:
        ex, rt = self.ex, self.ex.runtime
        if admission != self.gate or rt.workplace is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        await ex.verify()
        owner, guard, files = book(rt), Release(rt), rt.workplace.files
        attempt = await owner.attempts_repo(admission.scope).get(admission.attempt_id)
        if (
            attempt is None
            or attempt.state != "submitted"
            or attempt.session_id is None
            or attempt.operation_id != admission.operation_id
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        await guard.verify(admission, attempt.inputs, attempt.input_files, "api_transfer")
        a = request.arguments
        key = derived_id("helper", admission.scope, attempt.id, request.call_id)
        if request.operation == "save_script":
            if set(a) - {"file_id", "body", "expected_version"} or not a.get("body", "").strip():
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            try:
                version = int(a["expected_version"]) if "expected_version" in a else None
                data = a["body"].encode("utf-8")
                file_id = a["file_id"]
                name = hashlib.sha256(file_id.encode()).hexdigest()[:32]
                change = FileWrite.model_validate(
                    dict(
                        request_id=key,
                        file_id=file_id,
                        expected_version=version,
                        area=rt.session_output_area,
                        path="scripts/" + name + ".py",
                        title="Python helper",
                        kind="script",
                        provenance="generated",
                        media_type="text/x-python",
                        checksum=hashlib.sha256(data).hexdigest(),
                        size_bytes=len(data),
                        attempt_id=attempt.id,
                    )
                )
            except ValueError, KeyError:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
            entry = await files.write(admission, change, data)
            attempt = await owner.extend_inputs(
                attempt, files=(entry.reference,), written=(entry.reference,)
            )
            await guard.verify(admission, attempt.inputs, attempt.input_files, "api_transfer")
            await ex.verify()
            return json.dumps({"id": entry.id, "version": entry.version})
        if request.operation != "run_script" or set(a) - {"file_id", "version", "inputs", "argv"}:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        await self.runner.authorize(admission, self.template.id)
        fingerprint = hashlib.sha256(json.dumps(a, sort_keys=True).encode()).hexdigest()
        if any(
            r.status in ("prepared", "unknown") and r.call_id != request.call_id
            for r in attempt.helper_runs
        ):
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        existing = next((r for r in attempt.helper_runs if r.call_id == request.call_id), None)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
            if existing.status in ("prepared", "unknown"):
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            await guard.verify(admission, (), existing.outputs, "api_transfer")
            return self.receipt(existing)
        try:
            version = int(a["version"])
            inputs = json.loads(a.get("inputs", "[]"))
            argv = json.loads(a.get("argv", "[]"))
            if (
                not isinstance(inputs, list)
                or len(inputs) > 8
                or not isinstance(argv, list)
                or len(argv) > 16
                or any(not isinstance(s, str) or len(s) > 512 or "\0" in s for s in argv)
                or any(
                    not isinstance(r, dict)
                    or set(r) != {"id", "version"}
                    or not isinstance(r["id"], str)
                    or type(r["version"]) is not int
                    or r["version"] < 1
                    for r in inputs
                )
            ):
                raise ValueError()
            requested = [(a["file_id"], version), *((r["id"], r["version"]) for r in inputs)]
        except ValueError, KeyError, TypeError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        refs, sources = [], []
        self.required_areas.clear()
        assert rt.session_output_area is not None
        self.required_areas.add(rt.session_output_area)
        for index, (identifier, wanted) in enumerate(requested):
            entry, _ = await files.read(admission, identifier, wanted)
            if index == 0 and entry.kind != "script":
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            refs.append(entry.reference)
            self.required_areas.add(entry.area)
            sources += entry.dependencies
        await guard.verify(admission, sources, refs, "api_transfer")
        await self.runner.authorize(admission, self.template.id)
        attempt = await owner.extend_inputs(attempt, sources=sources, files=refs)
        ex.extra_sources = tuple(attempt.inputs)
        run = HelperRun(call_id=request.call_id, fingerprint=fingerprint, status="prepared")
        await ex.verify()
        # Same owner's transaction is the durable reservation before any container effect.
        task = RunnerTask(
            self.template.id, (*PREFIX, *argv), tuple(refs), self.template.ceiling, network="none"
        )
        self.template.check(task)
        attempt = await owner.helper_receipt(attempt, run)
        contents: dict[RunnerOutput, bytes] = {}
        try:
            outcome = await self.runner.run(admission, task)
            await ex.verify()
            await guard.verify(admission, attempt.inputs, attempt.input_files, "api_transfer")
            for output in outcome.outputs:
                renamed = RunnerOutput(
                    "helpers/" + key + "/files/" + output.name, output.checksum, output.size_bytes
                )
                contents[renamed] = await self.runner.staged(admission, output)
            if outcome.stdout_tail and outcome.status not in ("unknown", "cancelled"):
                data = outcome.stdout_tail.encode("utf-8")
                contents[
                    RunnerOutput(
                        "helpers/" + key + "/diagnostics/stdout.txt",
                        hashlib.sha256(data).hexdigest(),
                        len(data),
                    )
                ] = data

            class Staged:
                async def staged(self, supplied: WorkAdmission, output: RunnerOutput) -> bytes:
                    if supplied != admission or output not in contents:
                        raise ContractError(ErrorCode.ACCESS_DENIED)
                    return contents[output]

            async def authorize_publication() -> None:
                await self.runner.authorize(admission, self.template.id)

            check = await publication_check(rt, admission, authorize_publication)
            with rt.store.authorize_writes(
                check, owners=frozenset({Owner.CATALOGUE, Owner.ACTIVITY})
            ):
                saved = await save_outputs(
                    ex, cast(AgentHarness, Staged()), admission, attempt, tuple(contents)
                )
                await guard.verify(admission, attempt.inputs, saved, "api_transfer")
                await ex.verify()
                await authorize_publication()
                completed = run.model_copy(
                    update={"status": outcome.status, "outputs": list(saved)}
                )
                latest = await owner.attempts_repo(admission.scope).get(attempt.id)
                if latest is None:
                    raise ContractError(ErrorCode.STALE_CONTEXT)
                await owner.helper_receipt(latest, completed)
            rt.notify("work.changed", admission.scope, attempt.id)
            return self.receipt(completed)
        finally:
            contents.clear()
            await self.runner.cancel(admission)

    @staticmethod
    def receipt(run: HelperRun) -> str:
        return json.dumps(
            {
                "status": run.status,
                "outputs": [{"id": ref.artifact_id, "version": ref.version} for ref in run.outputs],
            }
        )
