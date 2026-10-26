"""W4 operator view of persona homes: maps an authorized scope to its volume, nothing more.

The access surface has already checked the operator session and the selected scope. Reading
is read-only; the only write is a new file in the home's inbox/. No right is granted.
"""

import base64
from typing import TYPE_CHECKING

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode

from .work_session_home import volume

if TYPE_CHECKING:
    from ai_agent_adapters.sandbox.home_files import HomeFiles

    from .service import Runtime

MAX_TEXT = 200_000


class HomeView:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    def _files(self) -> HomeFiles:
        files = self.rt.session_home_files
        if files is None:
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        return files

    async def list(self, scope: Scope, path: str) -> dict[str, object] | None:
        files = self._files()  # ContractError is a ValueError: keep it out of the try
        try:
            found = await files.list(volume(scope), path)
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if found.get("status") != "ok":
            return None  # missing, a link or not a directory: the same answer
        return {"entries": found["entries"], "complete": found["complete"]}

    async def read(self, scope: Scope, path: str) -> dict[str, object] | None:
        files = self._files()  # ContractError is a ValueError: keep it out of the try
        try:
            found = await files.read(volume(scope), path)
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if found.get("status") != "ok":
            return None
        data = base64.b64decode(found["content"])
        size = found["size"]
        truncated = len(data) < size
        try:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                # A cut at the read limit may split the last character; that is still text.
                if not truncated or error.start < len(data) - 3:
                    raise
                text = data[: error.start].decode("utf-8")
            if "\x00" in text:
                raise ValueError()
        except ValueError:
            # A binary file is a card (size), never raw bytes in the viewer.
            return {"text": None, "size": size, "truncated": False}
        return {
            "text": text[:MAX_TEXT],
            "size": size,
            "truncated": truncated or len(text) > MAX_TEXT,
        }

    async def put(self, scope: Scope, name: str, data: bytes) -> dict[str, object] | None:
        files = self._files()  # ContractError is a ValueError: keep it out of the try
        try:
            done = await files.put(volume(scope), name, data)
        except ValueError:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS) from None
        if done.get("status") == "exists":
            raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
        if done.get("status") != "ok":
            return None  # no home yet: the agent has not worked in it
        return {"path": "inbox/" + name, "size": len(data)}
