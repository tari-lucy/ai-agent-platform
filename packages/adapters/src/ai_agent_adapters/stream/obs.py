"""OBS transport (doc37 A3): obs-websocket v5, read state and switch scenes. Nothing else.

Starting and stopping a stream or a recording is the operator's act (D92), so those requests do
not exist here: a new capability is a new method on purpose, not a generic ``request`` that
instruction text could reach. The program runs on the operator's PC and is reached on loopback
through the reverse tunnel. The password comes from the explicitly loaded host environment and
never appears in an error; errors are fixed codes (``obs:<what>``).
"""

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

PASSWORD_SETTING = "AI_PLATFORM_OBS_PASSWORD"
RPC_VERSION = 1
MAX_FRAME = 1024 * 1024
WIRE_LOG = logging.Logger("ai_agent_adapters.stream.obs.wire")
WIRE_LOG.disabled = True
WIRE_LOG.propagate = False


class ObsError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ObsConfig:
    url: str = "ws://127.0.0.1:4455"
    connect_timeout: float = 5.0
    request_timeout: float = 5.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("ws", "wss") or parsed.hostname not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise ValueError("obs url must be ws on loopback (the tunnel puts the program there)")


@dataclass(frozen=True)
class ObsState:
    scene: str
    scenes: tuple[str, ...]
    streaming: bool
    recording: bool


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The obs-websocket v5 answer to the server's challenge."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsPort:
    def __init__(self, config: ObsConfig, password: SecretStr | None = None) -> None:
        self.config, self._password = config, password
        self._socket: ClientConnection | None = None

    async def _recv(self, socket: ClientConnection) -> dict[str, Any]:
        message = json.loads(await socket.recv())
        if not isinstance(message, dict) or not isinstance(message.get("d"), dict):
            raise ObsError("obs:protocol")
        return message

    async def connect(self) -> None:
        await self.close()
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                socket = await connect(
                    self.config.url,
                    max_size=MAX_FRAME,
                    open_timeout=self.config.connect_timeout,
                    logger=WIRE_LOG,
                    proxy=None,
                )
        except WebSocketException, OSError, TimeoutError:
            raise ObsError("obs:connect") from None
        self._socket = socket
        try:
            async with asyncio.timeout(self.config.request_timeout):
                hello = await self._recv(socket)
                if hello.get("op") != 0:
                    raise ObsError("obs:protocol")
                identify: dict[str, Any] = {"rpcVersion": RPC_VERSION}
                challenge = hello["d"].get("authentication")
                if isinstance(challenge, dict):
                    if self._password is None:
                        raise ObsError("obs:password-required")
                    identify["authentication"] = auth_string(
                        self._password.get_secret_value(),
                        str(challenge.get("salt", "")),
                        str(challenge.get("challenge", "")),
                    )
                await socket.send(json.dumps({"op": 1, "d": identify}))
                answer = await self._recv(socket)
                if answer.get("op") != 2:
                    raise ObsError("obs:protocol")
        except ObsError:
            await self.close()
            raise
        except TimeoutError:
            await self.close()
            raise ObsError("obs:timeout") from None
        except WebSocketException, OSError, ValueError:
            # A refused password closes the socket right after Identify.
            await self.close()
            raise ObsError("obs:denied") from None

    async def _request(self, kind: str, data: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        socket = self._socket
        if socket is None:
            raise ObsError("obs:closed")
        identifier = uuid4().hex
        frame = {"op": 6, "d": {"requestType": kind, "requestId": identifier, "requestData": data}}
        try:
            async with asyncio.timeout(self.config.request_timeout):
                await socket.send(json.dumps(frame))
                while True:
                    message = await self._recv(socket)
                    if message.get("op") == 7 and message["d"].get("requestId") == identifier:
                        break
        except TimeoutError:
            await self.close()
            raise ObsError("obs:timeout") from None
        except WebSocketException, OSError, ValueError:
            await self.close()
            raise ObsError("obs:transport") from None
        status = message["d"].get("requestStatus")
        if not isinstance(status, dict) or status.get("result") is not True:
            code = status.get("code") if isinstance(status, dict) else None
            raise ObsError(f"obs:request:{code if isinstance(code, int) else 'unknown'}")
        body = message["d"].get("responseData")
        return body if isinstance(body, dict) else {}

    async def state(self) -> ObsState:
        scenes = await self._request("GetSceneList")
        stream = await self._request("GetStreamStatus")
        record = await self._request("GetRecordStatus")
        return ObsState(
            scene=str(scenes.get("currentProgramSceneName", "")),
            scenes=tuple(
                str(s["sceneName"])
                for s in scenes.get("scenes", [])
                if isinstance(s, dict) and isinstance(s.get("sceneName"), str)
            ),
            streaming=stream.get("outputActive") is True,
            recording=record.get("outputActive") is True,
        )

    async def set_scene(self, name: str) -> None:
        await self._request("SetCurrentProgramScene", {"sceneName": name})

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.close()
            except WebSocketException, OSError:
                pass
