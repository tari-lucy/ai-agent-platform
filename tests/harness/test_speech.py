"""Live speech: one PCM stream to the output and the mouth, STOP, limits, money, journal."""

import asyncio
import json
import math
from array import array
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ai_agent_application.avatar import Avatar, ModelInfo, ParameterRange, profile_from_json
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_speech import MAX_TEXT, Speaker

RATE = 24000
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def window(amplitude=0.5):
    n = RATE * 60 // 1000
    return array("h", (int(32767 * amplitude * math.sin(n_ / 4)) for n_ in range(n))).tobytes()


def synth(count=5):
    async def make(text):
        for _ in range(count):
            await asyncio.sleep(0)
            yield window()

    return make


class Sink:
    def __init__(self, fail=None):
        self.got, self.ended, self.fail = b"", None, fail

    async def play(self, chunks):
        try:
            async for chunk in chunks:
                self.got += chunk
            self.ended = "end"
        except asyncio.CancelledError:
            self.ended = "aborted"
            raise
        if self.fail:
            raise self.fail


class Rig:
    def __init__(self):
        self.mouth = []

    async def connect(self):
        return ModelInfo("T", parameters={"MouthOpen": ParameterRange(0, 1)})

    async def hotkey(self, hotkey_id):
        pass

    async def expression(self, file, *, active):
        pass

    async def inject(self, values):
        self.mouth.append(values["MouthOpen"])

    async def close(self):
        pass


class Usage:
    def __init__(self, left=10.0):
        self.rows, self.left = [], left

    def remaining(self):
        return self.left

    def record_amount(self, amount, *, source):
        self.rows.append((round(amount, 8), source))


def speaker(tmp_path, **kwargs):
    kwargs.setdefault("synth", synth())
    kwargs.setdefault("sink", Sink())
    return Speaker(rate=RATE, journal=tmp_path / "speech.jsonl", clock=lambda: NOW, **kwargs)


def journal(tmp_path):
    return [json.loads(x) for x in (tmp_path / "speech.jsonl").read_text().splitlines()]


def say(spk, text="Привет!", current=lambda: None):
    return asyncio.run(spk.speak(text, current))


def test_one_stream_reaches_the_output_and_the_mouth_and_is_paid_and_journaled(tmp_path):
    rig, sink, usage = Rig(), Sink(), Usage()
    avatar = Avatar(rig, profile_from_json({"version": 1, "mouth_parameter": "MouthOpen"}))
    spk = speaker(tmp_path, sink=sink, avatar=avatar, usage=usage, hour_rate=0.12)
    assert say(spk) == "Сказано."
    assert len(sink.got) == 5 * len(window()) and sink.ended == "end"
    assert rig.mouth and rig.mouth[-1] == 0.0  # it moved, and it was closed at the end
    seconds = 5 * 0.06
    assert usage.rows == [(round(seconds / 3600 * 0.12, 8), "speech")]
    row = journal(tmp_path)[0]
    assert row["status"] == "spoken" and row["seconds"] == 0.3 and row["text"] == "Привет!"


def test_stop_cuts_output_and_mouth_and_is_raised_not_spoken_about(tmp_path):
    rig, sink = Rig(), Sink()
    avatar = Avatar(rig, profile_from_json({"version": 1, "mouth_parameter": "MouthOpen"}))
    spk = speaker(tmp_path, synth=synth(50), sink=sink, avatar=avatar)
    seen = []

    def current():
        seen.append(1)
        if len(seen) > 4:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    with pytest.raises(ContractError):
        say(spk, current=current)
    assert sink.ended == "aborted" and len(sink.got) < 50 * len(window())
    assert rig.mouth[-1] == 0.0
    assert journal(tmp_path)[0]["status"] == "stopped"


def test_refusals_do_not_synthesise_or_spend(tmp_path):
    usage = Usage(left=0.0)

    async def never(text):
        raise AssertionError("must not synthesise")
        yield b""

    spk = speaker(tmp_path, synth=never, usage=usage, daily_chars=100)
    assert "пуст" in say(spk, "   ")
    assert "Слишком длинно" in say(spk, "я" * (MAX_TEXT + 1))
    assert "бюджет" in say(spk)
    spk.usage = None
    (tmp_path / "speech.jsonl").write_text(
        json.dumps({"at": NOW.isoformat(), "status": "spoken", "chars": 95}) + "\n"
    )
    assert "лимит" in say(spk, "Привет, как дела?")
    assert usage.rows == []


def test_a_failing_output_is_a_plain_message_and_still_journaled_and_paid(tmp_path):
    from ai_agent_adapters.voice.pcm_bridge import BridgeError

    usage = Usage()
    sink = Sink(fail=BridgeError("bridge:connect"))
    out = say(speaker(tmp_path, sink=sink, usage=usage, hour_rate=1.0))
    assert "bridge:connect" in out and "Звук на компьютере" in out
    assert journal(tmp_path)[0]["status"] == "failed" and usage.rows


def test_second_phrase_while_speaking_is_refused():
    async def scenario(tmp_path):
        spk = speaker(tmp_path, synth=synth(20))
        first = asyncio.create_task(spk.speak("раз", lambda: None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert "ещё говорю" in await spk.speak("два", lambda: None)
        assert await first == "Сказано."

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        asyncio.run(scenario(Path(folder)))


def test_mcp_lists_speak_only_with_a_speaker_and_a_stopped_run_is_refused(tmp_path):
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    def ask(mcp, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return asyncio.run(mcp.handle(body))["result"]

    names = lambda m: {t["name"] for t in ask(m, "tools/list")["tools"]}  # noqa: E731
    assert "speak" not in names(PlatformMcp(runtime(), lambda: 3))  # type: ignore[arg-type]
    sink = Sink()
    mcp = PlatformMcp(runtime(), lambda: 3, speaker=speaker(tmp_path, sink=sink))  # type: ignore[arg-type]
    assert "speak" in names(mcp)
    call = {"name": "speak", "arguments": {"text": "Привет"}}
    assert ask(mcp, "tools/call", call)["content"][0]["text"] == "Сказано."
    stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, speaker=speaker(tmp_path))  # type: ignore[arg-type]
    assert ask(stopped, "tools/call", call)["content"][0]["text"] == "Отказ: STALE_CONTEXT"


def test_configure_wants_voice_and_port_together(monkeypatch, tmp_path):
    from ai_agent_core.local_host import harness, layout
    from ai_agent_core.local_host.layout import HostError

    class Config:
        work_sessions = object()

        def __init__(self, harness_config=None):
            self.harness = harness_config

        def model_copy(self, update):
            return Config(update["harness"])

    monkeypatch.setattr(layout, "paths", lambda root: root)
    monkeypatch.setattr(harness, "load_config", lambda host: Config())
    monkeypatch.setattr(harness, "save_config", lambda host, config: None)
    monkeypatch.setattr(harness, "password", lambda host: "")
    with pytest.raises(HostError):
        harness.configure(tmp_path, speech_voice="Kayla")
    on = harness.configure(tmp_path, speech_voice="Kayla", speech_port=8003, speech_hour_rate=0.12)
    assert (on.speech_voice, on.speech_port, on.speech_hour_rate) == ("Kayla", 8003, 0.12)
    monkeypatch.setattr(harness, "load_config", lambda host: Config(on))
    off = harness.configure(tmp_path, remove_speech=True)
    assert off.speech_voice is None and off.speech_port is None
