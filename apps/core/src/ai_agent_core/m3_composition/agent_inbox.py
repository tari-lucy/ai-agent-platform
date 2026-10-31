"""The operator hands a file to the persona: it lands in her workspace folder `inbox/`.

The platform writes into the host folder that is bind-mounted as /workspace; the persona's
container sees the file at /workspace/inbox/<name>. This grants no right and starts nothing.
Only the active persona's own folder, a safe file name, never an overwrite.
"""

import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

if TYPE_CHECKING:
    from .service import Runtime

MAX_NAME = 180
UNSAFE = re.compile(r'[\x00-\x1f<>:"|?*\\/]')


def safe_name(name: str) -> str:
    """A bare file name: no directories, no control or reserved characters, not hidden."""
    base = re.split(r"[\\/]", name.strip())[-1]
    base = UNSAFE.sub("_", base).strip(" .")
    if not base or base in {".", ".."}:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    stem, dot, extension = base.rpartition(".")
    if not dot:
        stem, extension = base, ""
    room = MAX_NAME - (len(extension) + 1 if extension else 0)
    return stem[:room] + ("." + extension if extension else "")


def store(folder: Path, name: str, content: bytes) -> str:
    """Write beside the final name, then link it in: an existing file is never replaced."""
    inbox = folder / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    part = inbox / f".part-{uuid.uuid4().hex}"
    part.write_bytes(content)
    os.chmod(part, 0o664)  # the persona's container user must be able to move and edit it
    try:
        for number in range(1, 200):
            final = name if number == 1 else f"{stem} ({number}){'.' + extension if dot else ''}"
            try:
                os.link(part, inbox / final)
                return final
            except FileExistsError:
                continue
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    finally:
        part.unlink(missing_ok=True)


class AgentInbox:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    async def put(self, scope: Scope, name: str, content: bytes) -> dict[str, object]:
        host = self.rt.harness
        if host is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if scope.persona_id != self.rt.supervisor.snapshot.scope.persona_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)  # only the active persona's folder
        folder = host.folders()
        if folder is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        if not content:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        try:
            final = await asyncio.to_thread(store, folder, safe_name(name), content)
        except OSError:
            raise ContractError(ErrorCode.SERVICE_UNAVAILABLE) from None
        return {"name": final, "path": f"/workspace/inbox/{final}", "size": len(content)}
