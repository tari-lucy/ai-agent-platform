"""Publish staged session files only through the existing catalogue owner."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ai_agent_application.agent_workplace.ports import AgentHarness, RunnerOutput, WorkAdmission
from ai_agent_application.memory import derived_id
from ai_agent_contracts.agent_workplace import FileWrite, WorkAttempt, logical_path
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef
from ai_agent_contracts.states import ErrorCode

if TYPE_CHECKING:
    from .service import Execution


async def save_outputs(
    ex: Execution,
    harness: AgentHarness,
    admission: WorkAdmission,
    attempt: WorkAttempt,
    outputs: Sequence[RunnerOutput],
) -> tuple[ArtifactRef, ...]:
    if not outputs:
        return ()
    rt = ex.runtime
    if rt.workplace is None or rt.session_output_area is None or attempt.session_id is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if len(outputs) + len(attempt.input_files) > 200 or len({o.name for o in outputs}) != len(
        outputs
    ):
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    paths = []
    try:
        for output in outputs:
            # Each attempt publishes immutable results; a later result.md cannot
            # overwrite (or collide with) an earlier turn's file without explicit CAS.
            paths.append(logical_path(attempt.id + "/" + output.name))
    except ValueError:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
    saved = []
    for path, output in zip(paths, outputs, strict=True):
        await ex.verify()
        data = await harness.staged(admission, output)
        await ex.verify()
        identifier = derived_id("work-file", admission.scope, attempt.id, output.name)
        change = FileWrite.model_validate(
            dict(
                request_id=derived_id("work-save", admission.scope, attempt.id, output.name),
                file_id=identifier,
                expected_version=None,
                area=rt.session_output_area,
                path=path,
                title=output.name[:200],
                kind="result",
                provenance="generated",
                media_type={
                    "md": "text/markdown",
                    "txt": "text/plain",
                    "json": "application/json",
                    "py": "text/x-python",
                }.get(output.name.rsplit(".", 1)[-1], "application/octet-stream"),
                checksum=output.checksum,
                size_bytes=output.size_bytes,
                attempt_id=attempt.id,
            )
        )
        entry = await rt.workplace.files.write(admission, change, data)
        saved.append(entry.reference)
    return tuple(saved)
