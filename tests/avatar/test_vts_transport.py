"""The VTube Studio transport against a fake program on a real loopback WebSocket."""

import asyncio
import json
import logging
import stat

import pytest
from ai_agent_adapters.avatar.vts import VtsConfig, VtsPort
from ai_agent_application.avatar import AvatarError
from websockets.asyncio.server import serve

QUIET = logging.Logger("fake-vts")
QUIET.disabled = True
TOKEN = "synthetic-plugin-token"


def program(seen, *, approve=True, loaded=True):
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind, data = msg["messageType"], msg["data"]
            seen.append((kind, data))
            if kind == "AuthenticationTokenRequest":
                out = {"authenticationToken": TOKEN} if approve else None
            elif kind == "AuthenticationRequest":
                out = {"authenticated": data["authenticationToken"] == TOKEN}
            elif kind == "CurrentModelRequest":
                out = {"modelLoaded": loaded, "modelName": "Test", "modelID": "m"}
            elif kind == "HotkeysInCurrentModelRequest":
                out = {"availableHotkeys": [{"name": "Smile", "hotkeyID": "h1"}, {"bad": 1}]}
            elif kind == "ExpressionStateRequest":
                out = {"expressions": [{"file": "a.exp3.json", "active": False}]}
            elif kind == "InputParameterListRequest":
                out = {
                    "defaultParameters": [{"name": "MouthOpen", "min": 0, "max": 1}],
                    "customParameters": [{"name": "Own", "min": -1, "max": 1}, {"name": "x"}],
                }
            else:
                out = {}
            reply = {"requestID": msg["requestID"], "messageType": kind + "Response", "data": out}
            if out is None:
                reply = {
                    "requestID": msg["requestID"],
                    "messageType": "APIError",
                    "data": {"errorID": 50, "message": "SECRET-REMOTE-TEXT"},
                }
            await ws.send(json.dumps(reply))

    return handler


def config(server, tmp_path, **kwargs):
    port = server.sockets[0].getsockname()[1]
    return VtsConfig(url=f"ws://127.0.0.1:{port}", token_path=tmp_path / "token", **kwargs)


def test_first_run_gets_a_token_then_reuses_it_and_discovers_the_rig(tmp_path):
    async def scenario():
        seen = []
        async with serve(program(seen), "127.0.0.1", 0, logger=QUIET) as server:
            cfg = config(server, tmp_path)
            port = VtsPort(cfg)
            info = await port.connect()
            assert info.name == "Test" and info.hotkeys == {"Smile": "h1"}
            assert info.expressions == {"a.exp3.json"}
            assert set(info.parameters) == {"MouthOpen", "Own"}
            token = tmp_path / "token"
            assert token.read_text() == TOKEN
            assert stat.S_IMODE(token.stat().st_mode) == 0o600
            kinds = [k for k, _ in seen]
            assert kinds.count("AuthenticationTokenRequest") == 1
            await port.close()
            seen.clear()
            await port.connect()  # the saved token is enough now
            assert "AuthenticationTokenRequest" not in [k for k, _ in seen]
            await port.hotkey("h1")
            await port.expression("a.exp3.json", active=True)
            await port.inject({"MouthOpen": 0.5})
            assert seen[-1][1]["parameterValues"] == [{"id": "MouthOpen", "value": 0.5}]
            assert seen[-1][1]["faceFound"] is False
            await port.close()

    asyncio.run(scenario())


def test_denied_plugin_and_empty_stage_give_fixed_codes_without_remote_text(tmp_path):
    async def scenario():
        async with serve(program([], approve=False), "127.0.0.1", 0, logger=QUIET) as server:
            with pytest.raises(AvatarError) as error:
                await VtsPort(config(server, tmp_path)).connect()
            assert error.value.code == "vts:api:50"
            assert "SECRET" not in str(error.value)
        async with serve(program([], loaded=False), "127.0.0.1", 0, logger=QUIET) as server:
            with pytest.raises(AvatarError) as error:
                await VtsPort(config(server, tmp_path)).connect()
            assert error.value.code == "vts:no-model"

    asyncio.run(scenario())


def test_unreachable_program_and_silence_are_fixed_codes(tmp_path):
    async def scenario():
        cfg = VtsConfig(url="ws://127.0.0.1:1", token_path=tmp_path / "t", connect_timeout=1)
        with pytest.raises(AvatarError) as error:
            await VtsPort(cfg).connect()
        assert error.value.code == "vts:connect"

        async def silent(ws):
            await asyncio.sleep(5)

        async with serve(silent, "127.0.0.1", 0, logger=QUIET) as server:
            (tmp_path / "token").write_text(TOKEN)  # a saved token: no approval wait
            cfg = config(server, tmp_path, request_timeout=0.3)
            with pytest.raises(AvatarError) as error:
                await VtsPort(cfg).connect()
            assert error.value.code == "vts:timeout"

    asyncio.run(scenario())


def test_remote_hosts_and_linked_tokens_are_refused(tmp_path):
    with pytest.raises(ValueError):
        VtsConfig(url="ws://203.0.113.5:8001")
    with pytest.raises(ValueError):
        VtsConfig(url="http://127.0.0.1:8001")
    assert VtsConfig(url="ws://203.0.113.5:8001", allow_remote=True)

    async def scenario():
        real = tmp_path / "real"
        real.write_text("x")
        (tmp_path / "token").symlink_to(real)
        async with serve(program([]), "127.0.0.1", 0, logger=QUIET) as server:
            with pytest.raises(AvatarError) as error:
                await VtsPort(config(server, tmp_path)).connect()  # a linked token is not used
            assert error.value.code == "vts:token"
        assert real.read_text() == "x"

    asyncio.run(scenario())


def test_avatar_over_the_real_transport_end_to_end(tmp_path):
    import math
    from array import array

    from ai_agent_application.avatar import Avatar, Emotion, drive, profile_from_json

    async def scenario():
        seen = []
        profile = profile_from_json(
            {
                "version": 1,
                "emotions": {"happy": {"hotkey": "Smile"}, "angry": {"expression": "a.exp3.json"}},
                "neutral": {"hotkey": "Smile"},
                "mouth_parameter": "MouthOpen",
            }
        )
        async with serve(program(seen), "127.0.0.1", 0, logger=QUIET) as server:
            avatar = Avatar(VtsPort(config(server, tmp_path)), profile)
            speech = array(
                "h", (int(20000 * math.sin(n / 5)) for n in range(1440 * 3))
            ).tobytes()  # three loud windows

            async def chunks():
                yield speech

            async def instant(_):
                return None

            await drive(avatar, chunks(), 24000, sleep=instant)
            assert (await avatar.express(Emotion.HAPPY)).applied
            await avatar.close()
        kinds = [k for k, _ in seen]
        assert kinds.count("InjectParameterDataRequest") >= 2  # a mouth frame and the closing one
        assert "HotkeyTriggerRequest" in kinds
        injected = [d for k, d in seen if k == "InjectParameterDataRequest"]
        assert injected[-1]["parameterValues"] == [{"id": "MouthOpen", "value": 0.0}]

    asyncio.run(scenario())
