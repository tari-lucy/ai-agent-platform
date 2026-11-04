"""The avatar_express tool: listed only when an avatar is configured, bound to the run."""

import asyncio
from types import SimpleNamespace

from ai_agent_application.avatar import Avatar, ModelInfo, profile_from_json
from ai_agent_contracts.states import RunState

from ai_agent_core.local_host.layout import HarnessConfig
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp


class Rig:
    def __init__(self):
        self.calls = []

    async def connect(self):
        return ModelInfo("T", hotkeys={"Smile": "h1"})

    async def hotkey(self, hotkey_id):
        self.calls.append(hotkey_id)

    async def expression(self, file, *, active):
        pass

    async def inject(self, values):
        pass

    async def close(self):
        pass


def runtime(state=RunState.ACTIVE, epoch=3):
    snapshot = SimpleNamespace(state=state, control_epoch=epoch, scope=None)
    return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)


def avatar(rig):
    profile = profile_from_json({"version": 1, "emotions": {"happy": {"hotkey": "Smile"}}})
    return Avatar(rig, profile)


def ask(mcp, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return asyncio.run(mcp.handle(body))["result"]


def express(mcp, emotion):
    return ask(mcp, "tools/call", {"name": "avatar_express", "arguments": {"emotion": emotion}})


def test_listed_only_when_configured():
    names = lambda m: {t["name"] for t in ask(m, "tools/list")["tools"]}  # noqa: E731
    assert "avatar_express" not in names(PlatformMcp(runtime(), lambda: 3))  # type: ignore[arg-type]
    mcp = PlatformMcp(runtime(), lambda: 3, avatar=avatar(Rig()))  # type: ignore[arg-type]
    tool = next(t for t in ask(mcp, "tools/list")["tools"] if t["name"] == "avatar_express")
    assert "happy" in tool["inputSchema"]["properties"]["emotion"]["enum"]


def test_call_shows_the_feeling_and_unknown_ones_are_refused():
    rig = Rig()
    mcp = PlatformMcp(runtime(), lambda: 3, avatar=avatar(rig))  # type: ignore[arg-type]
    assert express(mcp, "happy")["content"][0]["text"] == "Выражение: happy."
    assert rig.calls == ["h1"]
    assert express(mcp, "joyful")["content"][0]["text"] == "Отказ: INVALID_ARGUMENTS"
    without = PlatformMcp(runtime(), lambda: 3)  # type: ignore[arg-type]
    assert express(without, "happy")["content"][0]["text"] == "Отказ: CAPABILITY_UNAVAILABLE"


def test_a_stopped_run_cannot_move_the_face():
    rig = Rig()
    mcp = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, avatar=avatar(rig))  # type: ignore[arg-type]
    assert express(mcp, "happy")["content"][0]["text"] == "Отказ: STALE_CONTEXT"
    assert rig.calls == []


def test_avatar_settings_are_validated():
    assert HarnessConfig(avatar_port=8001, avatar_profile="opechatka.json").avatar_port == 8001
    for bad in ({"avatar_port": 80}, {"avatar_profile": "../x.json"}, {"avatar_profile": "a.txt"}):
        try:
            HarnessConfig(**bad)
        except ValueError:
            continue
        raise AssertionError(bad)


class FakeConfig:
    work_sessions = object()

    def __init__(self, harness=None):
        self.harness = harness

    def model_copy(self, update):
        return FakeConfig(update["harness"])


def test_configure_wants_both_port_and_profile_or_neither(monkeypatch, tmp_path):
    from ai_agent_core.local_host import harness, layout
    from ai_agent_core.local_host.layout import HostError

    saved = []
    monkeypatch.setattr(layout, "paths", lambda root: root)
    monkeypatch.setattr(harness, "load_config", lambda host: FakeConfig(None))
    monkeypatch.setattr(harness, "save_config", lambda host, config: saved.append(config.harness))
    monkeypatch.setattr(harness, "password", lambda host: "")

    try:
        harness.configure(tmp_path, avatar_port=8001)
    except HostError as error:
        assert "AVATAR_PORT_AND_PROFILE" in repr(error)
    else:
        raise AssertionError("a port alone must be refused")
    assert saved == []
    on = harness.configure(tmp_path, avatar_port=8001, avatar_profile="opechatka.json")
    assert (on.avatar_port, on.avatar_profile) == (8001, "opechatka.json")
    monkeypatch.setattr(harness, "load_config", lambda host: FakeConfig(on))
    off = harness.configure(tmp_path, remove_avatar=True)
    assert (off.avatar_port, off.avatar_profile) == (None, None)
    kept = harness.configure(tmp_path, avatar_profile="other.json")  # port kept from before
    assert (kept.avatar_port, kept.avatar_profile) == (8001, "other.json")
