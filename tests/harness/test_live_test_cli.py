"""`live-test` against fake programs: what the operator would see on the first try."""

import asyncio
import json
import logging
from types import SimpleNamespace

from websockets.asyncio.server import serve

from ai_agent_core.local_host.layout import HarnessConfig, HostPaths
from ai_agent_core.local_host.live_test_cli import check

QUIET = logging.Logger("fake-live-test")
QUIET.disabled = True
PROFILE = {
    "version": 1,
    "emotions": {"happy": {"hotkey": "Smile"}, "sad": {"hotkey": "Missing"}},
    "neutral": {"hotkey": "Reset"},
}


def vts(seen):
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg["messageType"]
            seen.append(kind)
            out = {
                "AuthenticationTokenRequest": {"authenticationToken": "t"},
                "AuthenticationRequest": {"authenticated": True},
                "CurrentModelRequest": {"modelLoaded": True, "modelName": "M"},
                "HotkeysInCurrentModelRequest": {
                    "availableHotkeys": [
                        {"name": "Smile", "hotkeyID": "1"},
                        {"name": "Reset", "hotkeyID": "0"},
                    ]
                },
                "ExpressionStateRequest": {"expressions": []},
                "InputParameterListRequest": {"defaultParameters": []},
            }.get(kind, {})
            reply = {"requestID": msg["requestID"], "messageType": kind + "Response", "data": out}
            await ws.send(json.dumps(reply))

    return handler


def lines():
    got: list[str] = []
    return got, got.append


def test_avatar_check_shows_each_emotion_and_reports_a_mismatch(tmp_path):
    async def scenario():
        seen: list[str] = []
        folder = tmp_path / "data" / "harness" / "avatar"
        folder.mkdir(parents=True)
        (folder / "a.json").write_text(json.dumps(PROFILE))
        async with serve(vts(seen), "127.0.0.1", 0, logger=QUIET) as server:
            port = server.sockets[0].getsockname()[1]
            settings = HarnessConfig(avatar_port=port, avatar_profile="a.json")
            got, out = lines()
            assert await check(
                "avatar", settings, HostPaths(tmp_path), None, pause=1.6, settle=0.1, out=out
            )
        text = "\n".join(got)
        assert "На связи" in text and "sad:hotkey:Missing" in text  # the mismatch is reported
        assert "happy: Выражение: happy." in got
        assert "sad: Для sad в этой модели нет выражения." in got  # not a pretended success
        assert "Лицо возвращено" in text and "HotkeyTriggerRequest" in seen

    asyncio.run(scenario())


def test_checks_say_plainly_what_is_missing(tmp_path):
    async def scenario():
        host = HostPaths(tmp_path)
        none = SimpleNamespace(secrets={}, binding=None)
        for what, hint in (
            ("avatar", "не настроен"),
            ("speak", "не настроен"),
            ("obs", "не настроен"),
        ):
            got, out = lines()
            assert await check(what, HarnessConfig(), host, none, out=out) is False
            assert hint in "\n".join(got)
        # configured, but nothing listens on the port: a hint about the usual causes
        folder = tmp_path / "data" / "harness" / "avatar"
        folder.mkdir(parents=True)
        (folder / "a.json").write_text(json.dumps(PROFILE))
        got, out = lines()
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]  # a port nothing listens on
        settings = HarnessConfig(avatar_port=free, avatar_profile="a.json")
        assert await check("avatar", settings, host, none, out=out) is False
        assert "туннель" in "\n".join(got)

    asyncio.run(scenario())
