"""VTube Studio transport (doc37, D89): the public WebSocket API, nothing else.

The program runs on the operator's PC and is reached on loopback through the reverse tunnel
(D90). The plugin token is a small local file, created on the first approval in the program and
never logged. Errors carry fixed codes only: no remote message, token, URL or model data.
"""

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ai_agent_application.avatar import AvatarError, ModelInfo, ParameterRange
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

API = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0"}
MAX_TOKEN = 400
MAX_FRAME = 1024 * 1024
APPROVAL_WAIT = 60.0  # seconds the operator has to click "allow" in the program
# websockets DEBUG logs include handshake headers; keep them out of the host logger.
WIRE_LOG = logging.Logger("ai_agent_adapters.avatar.vts.wire")
WIRE_LOG.disabled = True
WIRE_LOG.propagate = False


@dataclass(frozen=True)
class VtsConfig:
    url: str = "ws://127.0.0.1:8001"
    token_path: Path = Path("vts-token")
    plugin: str = "Opechatka"
    developer: str = "ai-agent-platform"
    connect_timeout: float = 5.0
    request_timeout: float = 10.0
    allow_remote: bool = False

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in ("ws", "wss") or parsed.hostname is None:
            raise ValueError("vts url")
        if not self.allow_remote and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("vts host must be loopback (the tunnel puts the program there)")


def _read_token(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOKEN:
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_token(path: Path, token: str) -> None:
    if path.is_symlink() or len(token) > MAX_TOKEN:
        raise AvatarError("vts:token")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        handle.write(token)


class VtsPort:
    def __init__(self, config: VtsConfig) -> None:
        self.config = config
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def _read(self, socket: ClientConnection) -> None:
        """Read every frame of the program; hand a reply to whoever waits for its request id.

        Frames nobody waits for (the answers to fire-and-forget parameter frames) are dropped
        here, so a slow link never queues them up. When the connection ends, waiters fail.
        """
        try:
            async for raw in socket:
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(reply, dict):
                    waiter = self._pending.pop(str(reply.get("requestID")), None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(reply)
        except WebSocketException, OSError:
            pass
        finally:
            waiters, self._pending = list(self._pending.values()), {}
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(AvatarError("vts:transport"))
            if self._socket is socket:
                self._socket = None

    async def _request(
        self,
        kind: str,
        data: Mapping[str, Any] | None = None,
        *,
        wait: float | None = None,
        reply: bool = True,
    ) -> dict[str, Any]:
        """One request. With ``reply=False`` it is only sent: a stream of parameter frames must
        not wait for a round trip each (through a tunnel that is 0.2 s, a 5 Hz mouth)."""
        socket = self._socket
        if socket is None:
            raise AvatarError("vts:closed")
        identifier = uuid4().hex
        frame = {**API, "requestID": identifier, "messageType": kind, "data": dict(data or {})}
        waiter: asyncio.Future[dict[str, Any]] | None = None
        if reply:
            waiter = asyncio.get_running_loop().create_future()
            self._pending[identifier] = waiter
        try:
            async with asyncio.timeout(wait or self.config.request_timeout):
                await socket.send(json.dumps(frame))
                if waiter is None:
                    return {}
                answer = await waiter
        except TimeoutError:
            self._pending.pop(identifier, None)
            await self.close()
            raise AvatarError("vts:timeout") from None
        except AvatarError:
            await self.close()
            raise
        except (WebSocketException, OSError, ValueError) as error:
            self._pending.pop(identifier, None)
            await self.close()
            raise AvatarError("vts:transport") from error
        data_back = answer.get("data")
        if answer.get("messageType") == "APIError" or not isinstance(data_back, dict):
            code = data_back.get("errorID") if isinstance(data_back, dict) else None
            raise AvatarError(f"vts:api:{code if isinstance(code, int) else 'unknown'}")
        return data_back

    async def connect(self) -> ModelInfo:
        await self.close()
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                self._socket = await connect(
                    self.config.url,
                    max_size=MAX_FRAME,
                    open_timeout=self.config.connect_timeout,
                    logger=WIRE_LOG,
                    proxy=None,
                )
        except (WebSocketException, OSError, TimeoutError) as error:
            self._socket = None
            raise AvatarError("vts:connect") from error
        self._reader = asyncio.get_running_loop().create_task(self._read(self._socket))
        await self._authenticate()
        return await self._discover()

    async def _authenticate(self) -> None:
        who = {"pluginName": self.config.plugin, "pluginDeveloper": self.config.developer}
        token = _read_token(self.config.token_path)
        if token is not None and await self._check(who, token):
            return
        # The program asks its user to allow the plugin; the first run waits for that click.
        reply = await self._request("AuthenticationTokenRequest", who, wait=APPROVAL_WAIT)
        token = reply.get("authenticationToken")
        if not isinstance(token, str) or not token:
            raise AvatarError("vts:denied")
        _write_token(self.config.token_path, token)
        if not await self._check(who, token):
            raise AvatarError("vts:denied")

    async def _check(self, who: Mapping[str, str], token: str) -> bool:
        reply = await self._request("AuthenticationRequest", {**who, "authenticationToken": token})
        return reply.get("authenticated") is True

    async def _discover(self) -> ModelInfo:
        model = await self._request("CurrentModelRequest")
        if model.get("modelLoaded") is not True:
            raise AvatarError("vts:no-model")
        hotkeys = await self._request("HotkeysInCurrentModelRequest")
        expressions = await self._request("ExpressionStateRequest", {"details": False})
        inputs = await self._request("InputParameterListRequest")
        parameters: dict[str, ParameterRange] = {}
        for entry in [*inputs.get("defaultParameters", []), *inputs.get("customParameters", [])]:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                low, high = entry.get("min"), entry.get("max")
                if isinstance(low, int | float) and isinstance(high, int | float):
                    parameters[entry["name"]] = ParameterRange(float(low), float(high))
        return ModelInfo(
            name=str(model.get("modelName", ""))[:120],
            hotkeys={
                h["name"]: h["hotkeyID"]
                for h in hotkeys.get("availableHotkeys", [])
                if isinstance(h, dict) and isinstance(h.get("name"), str) and h.get("hotkeyID")
            },
            expressions=frozenset(
                e["file"]
                for e in expressions.get("expressions", [])
                if isinstance(e, dict) and isinstance(e.get("file"), str)
            ),
            parameters=parameters,
        )

    async def hotkey(self, hotkey_id: str) -> None:
        await self._request("HotkeyTriggerRequest", {"hotkeyID": hotkey_id})

    async def expression(self, file: str, *, active: bool) -> None:
        await self._request(
            "ExpressionActivationRequest", {"expressionFile": file, "active": active}
        )

    async def inject(self, values: Mapping[str, float]) -> None:
        await self._request(
            "InjectParameterDataRequest",
            {
                # "found": with False the program shows its own "tracking lost" state and keeps
                # the eyes shut (checked on a rig without a camera).
                "faceFound": True,
                "mode": "set",
                "parameterValues": [{"id": k, "value": v} for k, v in values.items()],
            },
            reply=False,
        )

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        reader, self._reader = self._reader, None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if socket is not None:
            try:
                await socket.close()
            except WebSocketException, OSError:
                pass
