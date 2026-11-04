"""The first-guess profile and `avatar-probe` against a fake VTube Studio."""

import asyncio
import json
import logging

import pytest
from ai_agent_application.avatar import ModelInfo, ParameterRange, draft_profile, profile_from_json
from websockets.asyncio.server import serve

from ai_agent_core.local_host.avatar_cli import probe
from ai_agent_core.local_host.layout import HostError

QUIET = logging.Logger("fake-vts-probe")
QUIET.disabled = True
INFO = ModelInfo(
    "Opechatka",
    hotkeys={"Smile": "1", "Грусть": "2", "Shock face": "3", "Reset Pose": "4", "Misc": "5"},
    expressions=frozenset({"angry.exp3.json", "sleepy_eyes.exp3.json"}),
    parameters={"MouthOpen": ParameterRange(0, 1), "MouthSmile": ParameterRange(0, 1)},
)


def test_names_are_matched_in_english_and_russian_and_hotkeys_win():
    profile, notes = draft_profile(INFO)
    assert profile["emotions"] == {
        "happy": {"hotkey": "Smile"},
        "sad": {"hotkey": "Грусть"},
        "angry": {"expression": "angry.exp3.json"},
        "surprised": {"hotkey": "Shock face"},
        "sleepy": {"expression": "sleepy_eyes.exp3.json"},
    }
    assert (
        profile["neutral"] == {"hotkey": "Reset Pose"} and profile["mouth_parameter"] == "MouthOpen"
    )
    assert profile["model"] == "Opechatka"
    assert any(n.startswith("thinking") for n in notes) and any(
        n.startswith("embarrassed") for n in notes
    )
    assert profile_from_json(profile).validate(INFO) == []  # it fits the rig it came from


def test_a_model_with_nothing_recognisable_gives_an_honest_empty_draft():
    profile, notes = draft_profile(ModelInfo("", hotkeys={"A": "1"}))
    assert profile == {"version": 1, "emotions": {}}
    assert len(notes) == 9 and any("рот" in n for n in notes) and any("neutral" in n for n in notes)
    profile_from_json(profile)


def vts():
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind = msg["messageType"]
            out = {
                "AuthenticationTokenRequest": {"authenticationToken": "tok"},
                "AuthenticationRequest": {"authenticated": True},
                "CurrentModelRequest": {"modelLoaded": True, "modelName": "Opechatka"},
                "HotkeysInCurrentModelRequest": {
                    "availableHotkeys": [
                        {"name": n, "hotkeyID": i} for n, i in INFO.hotkeys.items()
                    ]
                },
                "ExpressionStateRequest": {"expressions": [{"file": f} for f in INFO.expressions]},
                "InputParameterListRequest": {
                    "defaultParameters": [{"name": n, "min": 0, "max": 1} for n in INFO.parameters]
                },
            }[kind]
            reply = {"requestID": msg["requestID"], "messageType": kind + "Response", "data": out}
            await ws.send(json.dumps(reply))

    return handler


def test_probe_prints_the_model_writes_a_draft_and_never_overwrites(tmp_path, capsys):
    async def scenario():
        async with serve(vts(), "127.0.0.1", 0, logger=QUIET) as server:
            port = server.sockets[0].getsockname()[1]
            assert await probe(tmp_path, port, "mine.json") == 0
            with pytest.raises(HostError):
                await probe(tmp_path, port, "mine.json")  # already there: refused
            for bad in ("../x.json", "x.txt", "a/b.json"):
                with pytest.raises(HostError):
                    await probe(tmp_path, port, bad)
            assert await probe(tmp_path, port, None) == 0

    asyncio.run(scenario())
    out = capsys.readouterr().out
    assert "Model: Opechatka" in out and "Smile" in out and "Draft written" in out
    draft = tmp_path / "data" / "harness" / "avatar" / "mine.json"
    assert profile_from_json(json.loads(draft.read_text())).mouth_parameter == "MouthOpen"
    assert (tmp_path / "data" / "harness" / "avatar" / "vts-token").read_text() == "tok"


def test_probe_with_no_program_is_a_fixed_error(tmp_path):
    with pytest.raises(HostError) as error:
        asyncio.run(probe(tmp_path, 1, None))
    assert "VTS_CONNECT" in repr(error.value)
