"""OBS: the v5 handshake with a password, state, scene switch, pacing and the missing powers."""

import asyncio
import base64
import hashlib
import inspect
import json
import logging
from types import SimpleNamespace

import pytest
from ai_agent_adapters.stream.obs import ObsConfig, ObsError, ObsPort, ObsState
from ai_agent_contracts.states import RunState
from pydantic import SecretStr
from websockets.asyncio.server import serve

from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_stage import Stage

QUIET = logging.Logger("fake-obs")
QUIET.disabled = True
PASSWORD, SALT, CHALLENGE = "synthetic-obs-password", "salt-123", "challenge-456"


def expected_auth():
    def b64(raw):
        return base64.b64encode(raw).decode()

    secret = b64(hashlib.sha256((PASSWORD + SALT).encode()).digest())
    return b64(hashlib.sha256((secret + CHALLENGE).encode()).digest())


def program(seen, *, password=True):
    async def handler(ws):
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}}
        if password:
            hello["d"]["authentication"] = {"challenge": CHALLENGE, "salt": SALT}
        await ws.send(json.dumps(hello))
        identify = json.loads(await ws.recv())
        if password and identify["d"].get("authentication") != expected_auth():
            await ws.close(4009, "Authentication failed")
            return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        scene = "Main"
        async for raw in ws:
            request = json.loads(raw)["d"]
            kind = request["requestType"]
            seen.append((kind, request.get("requestData")))
            body, result = {}, True
            if kind == "GetSceneList":
                body = {
                    "currentProgramSceneName": scene,
                    "scenes": [{"sceneName": "Main"}, {"sceneName": "Chat"}, {"x": 1}],
                }
            elif kind == "GetStreamStatus":
                body = {"outputActive": True}
            elif kind == "GetRecordStatus":
                body = {"outputActive": False}
            elif kind == "SetCurrentProgramScene":
                scene = request["requestData"]["sceneName"]
            else:
                result = False
            reply = {
                "requestType": kind,
                "requestId": request["requestId"],
                "requestStatus": {"result": result, "code": 100 if result else 204},
                "responseData": body,
            }
            await ws.send(json.dumps({"op": 7, "d": reply}))

    return handler


def cfg(server, **kwargs):
    return ObsConfig(url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", **kwargs)


def test_handshake_state_and_scene_switch(tmp_path):
    async def scenario():
        seen = []
        async with serve(program(seen), "127.0.0.1", 0, logger=QUIET) as server:
            port = ObsPort(cfg(server), SecretStr(PASSWORD))
            await port.connect()
            state = await port.state()
            assert state == ObsState("Main", ("Main", "Chat"), True, False)
            await port.set_scene("Chat")
            assert (await port.state()).scene == "Chat"
            await port.close()

    asyncio.run(scenario())


def test_wrong_or_missing_password_is_a_fixed_code():
    async def scenario():
        async with serve(program([]), "127.0.0.1", 0, logger=QUIET) as server:
            with pytest.raises(ObsError) as error:
                await ObsPort(cfg(server), SecretStr("wrong")).connect()
            assert error.value.code == "obs:denied" and "wrong" not in str(error.value)
            with pytest.raises(ObsError) as error:
                await ObsPort(cfg(server)).connect()
            assert error.value.code == "obs:password-required"
        async with serve(program([], password=False), "127.0.0.1", 0, logger=QUIET) as server:
            await ObsPort(cfg(server)).connect()  # no password set in OBS: fine

    asyncio.run(scenario())


def test_remote_hosts_unreachable_and_failed_requests():
    with pytest.raises(ValueError):
        ObsConfig(url="ws://203.0.113.5:4455")

    async def scenario():
        with pytest.raises(ObsError) as error:
            await ObsPort(ObsConfig(url="ws://127.0.0.1:1", connect_timeout=1)).connect()
        assert error.value.code == "obs:connect"
        async with serve(program([], password=False), "127.0.0.1", 0, logger=QUIET) as server:
            port = ObsPort(cfg(server))
            await port.connect()
            with pytest.raises(ObsError) as error:
                await port._request("Nonsense")  # noqa: SLF001
            assert error.value.code == "obs:request:204"

    asyncio.run(scenario())


def test_the_adapter_cannot_start_or_stop_a_broadcast():
    names = {n for n, _ in inspect.getmembers(ObsPort, inspect.isfunction)}
    assert not any("start" in n or "stop" in n or "record" in n for n in names)


class FakeObs:
    def __init__(self):
        self.scene, self.fail, self.calls = "Main", False, []

    async def connect(self):
        if self.fail:
            raise ObsError("obs:connect")

    async def state(self):
        if self.fail:
            raise ObsError("obs:transport")
        return ObsState(self.scene, ("Main", "Chat"), False, False)

    async def set_scene(self, name):
        self.calls.append(name)
        self.scene = name

    async def close(self):
        pass


class Clock:
    now = 50.0

    def __call__(self):
        return self.now


def test_stage_status_scene_pacing_and_loss():
    async def scenario():
        obs, clock = FakeObs(), Clock()
        stage = Stage(obs, clock=clock)
        status = await stage.status()
        assert "не идёт" in status and "«Chat»" in status
        assert "Нет сцены" in await stage.scene("Secret")
        assert "уже включена" in await stage.scene("Main")
        assert "Включена сцена «Chat»" in await stage.scene("Chat")
        assert "Слишком часто" in await stage.scene("Main")
        clock.now += 4
        assert "Включена" in await stage.scene("Main")
        assert obs.calls == ["Chat", "Main"]
        obs.fail = True
        assert "недоступен" in await stage.status()
        obs.fail = False
        assert "недоступен" in await stage.status()  # waits before trying again
        clock.now += 31
        assert "Эфир" in await stage.status()

    asyncio.run(scenario())


def test_stage_tools_follow_the_run():
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    def call(mcp, name, arguments=None):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        return asyncio.run(mcp.handle(body))["result"]["content"][0]["text"]

    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    def names(mcp):
        return {t["name"] for t in asyncio.run(mcp.handle(listing))["result"]["tools"]}

    bare = PlatformMcp(runtime(), lambda: 3)  # type: ignore[arg-type]
    assert not {"stream_status", "stream_scene"} & names(bare)
    assert call(bare, "stream_status") == "Отказ: CAPABILITY_UNAVAILABLE"
    mcp = PlatformMcp(runtime(), lambda: 3, stage=Stage(FakeObs()))  # type: ignore[arg-type]
    assert {"stream_status", "stream_scene"} <= names(mcp)
    assert "Эфир" in call(mcp, "stream_status")
    assert "Включена" in call(mcp, "stream_scene", {"scene": "Chat"})
    stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, stage=Stage(FakeObs()))  # type: ignore[arg-type]
    assert call(stopped, "stream_status") == "Отказ: STALE_CONTEXT"
