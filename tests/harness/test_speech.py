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


class RealTimeSink:
    """A consumer paced like the audio device: it takes a chunk per `pace` seconds."""

    def __init__(self, pace):
        self.pace, self.got, self.ended = pace, b"", None

    async def play(self, chunks):
        try:
            async for chunk in chunks:
                await asyncio.sleep(self.pace)
                self.got += chunk
            self.ended = "end"
        except asyncio.CancelledError:
            self.ended = "aborted"
            raise


def test_synthesis_faster_than_playback_is_held_back_not_cut(tmp_path):
    """Soniox answers in a second what takes five to play: the phrase must not be cut."""
    sink = RealTimeSink(0.01)
    spk = speaker(tmp_path, synth=synth(150), sink=sink, avatar=None)  # 150 chunks > the queue
    assert say(spk) == "Сказано."
    assert len(sink.got) == 150 * len(window()) and sink.ended == "end"


def test_a_consumer_that_takes_nothing_cuts_the_phrase_after_the_stuck_time(tmp_path):
    sink = RealTimeSink(30)  # never gets to the second chunk within the test
    spk = speaker(tmp_path, synth=synth(500), sink=sink, stuck_after=0.3)
    out = say(spk)
    assert "не успевал" in out and sink.ended == "aborted"
    assert journal(tmp_path)[0]["status"] == "cut"


def test_stop_is_noticed_while_the_buffered_audio_is_still_playing(tmp_path):
    """The synthesis finished at once; STOP arrives during playback and aborts it."""
    sink = RealTimeSink(0.05)
    spk = speaker(tmp_path, synth=synth(10), sink=sink)
    started = {}

    def current():
        import time

        started.setdefault("t", time.monotonic())
        if time.monotonic() - started["t"] > 0.25:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    with pytest.raises(ContractError):
        say(spk, current=current)
    assert sink.ended == "aborted" and len(sink.got) < 10 * len(window())
    assert journal(tmp_path)[0]["status"] == "stopped"


def test_the_mouth_waits_for_the_sound_by_the_configured_delay(tmp_path):
    import time

    first = {}

    class Rig2(Rig):
        async def inject(self, values):
            first.setdefault("mouth", time.monotonic())
            await super().inject(values)

    class TimedSink(Sink):
        async def play(self, chunks):
            async for chunk in chunks:
                first.setdefault("sound", time.monotonic())
                self.got += chunk

    avatar = Avatar(Rig2(), profile_from_json({"version": 1, "mouth_parameter": "MouthOpen"}))
    spk = speaker(tmp_path, sink=TimedSink(), avatar=avatar, mouth_delay=0.3)
    assert say(spk) == "Сказано."
    assert first["mouth"] - first["sound"] >= 0.25


def test_speaking_stays_true_for_a_few_seconds_after_her_voice_ends(tmp_path):
    clock = {"t": 100.0}
    spk = speaker(tmp_path, monotonic=lambda: clock["t"])
    assert spk.speaking is False
    assert say(spk) == "Сказано."
    assert spk.speaking is True  # the ears may still catch her
    clock["t"] += 2.9
    assert spk.speaking is True
    clock["t"] += 0.2
    assert spk.speaking is False


def test_the_voice_waits_for_a_lead_so_it_does_not_run_dry(tmp_path):
    """Chunks come in bursts; playback starts only when a second of sound is in hand."""

    async def scenario():
        produced = 0
        first_seen_after = None

        async def bursts(text):
            nonlocal produced
            for _ in range(30):  # 30 windows of 60 ms = 1.8 s
                await asyncio.sleep(0)
                produced += 1
                yield window()

        class Watching(Sink):
            async def play(self, chunks):
                nonlocal first_seen_after
                async for chunk in chunks:
                    if first_seen_after is None:
                        first_seen_after = produced
                    self.got += chunk
                self.ended = "end"

        sink = Watching()
        speaker = Speaker(bursts, sink, RATE, tmp_path / "s.jsonl", prebuffer=1.0)
        assert await speaker.speak("привет", lambda: None) == "Сказано."
        return first_seen_after, len(sink.got)

    first_seen_after, got = asyncio.run(scenario())
    assert first_seen_after >= 17  # 17 windows of 60 ms = 1.02 s gathered before the first byte
    assert got == 30 * len(window())  # and nothing lost


def test_a_phrase_shorter_than_the_lead_is_still_played_whole(tmp_path):
    async def scenario():
        sink = Sink()
        speaker = Speaker(synth(3), sink, RATE, tmp_path / "s.jsonl", prebuffer=1.0)
        assert await speaker.speak("да", lambda: None) == "Сказано."
        return len(sink.got)

    assert asyncio.run(scenario()) == 3 * len(window())


def quiet_avatar():
    return Avatar(Rig(), profile_from_json({"version": 1, "mouth_parameter": "MouthOpen"}))


def test_the_mouth_delay_follows_the_measured_lead_and_is_kept_between_runs(tmp_path):
    """The voice comes out later than the mouth (network, the computer's buffer): after a longer
    phrase the delay grows by the measured lead, and a new Speaker starts from that value."""

    class LateSink(Sink):
        async def play(self, chunks):
            await super().play(chunks)
            await asyncio.sleep(1.0)  # the computer plays the buffer out after the last frame

    async def scenario():
        sync = tmp_path / "sync.json"
        sink = LateSink()
        spk = Speaker(
            synth(5), sink, RATE, tmp_path / "s.jsonl", avatar=quiet_avatar(), sync_file=sync,
            mouth_delay=0.1, prebuffer=0.0, auto_delay=True,
        )  # fmt: skip
        spk.calibrate_min = 0.0  # a short test phrase counts
        before = spk.mouth_delay
        assert await spk.speak("Раз два три", lambda: None) == "Сказано."
        grown = spk.mouth_delay
        again = Speaker(
            synth(5), LateSink(), RATE, tmp_path / "s2.jsonl", sync_file=sync, auto_delay=True
        )
        return before, grown, again.mouth_delay

    before, grown, reloaded = asyncio.run(scenario())
    assert grown > before + 0.1  # it moved towards the lead, gently
    assert reloaded == pytest.approx(grown, abs=0.002)  # and it is remembered


def test_short_phrases_and_odd_leads_do_not_move_the_delay(tmp_path):
    spk = Speaker(synth(1), Sink(), RATE, tmp_path / "s.jsonl", mouth_delay=0.3, auto_delay=True)
    spk._adapt(1.0, 0.5)  # under the minimum length: too little to learn from
    assert spk.mouth_delay == 0.3
    spk._adapt(9.0, 5.0)  # an absurd lead (a hang, not a delay)
    spk._adapt(-9.0, 5.0)
    assert spk.mouth_delay == 0.3
    spk._adapt(-0.2, 5.0)  # the mouth ends late: the delay shrinks, never below zero
    assert 0.0 <= spk.mouth_delay < 0.3
    spk._adapt(0.0, 5.0)
    for _ in range(20):
        spk._adapt(-2.5, 5.0)
    assert spk.mouth_delay == 0.0
    spk._adapt(4.0, 5.0)  # one big lead moves it by the bounded step only
    assert spk.mouth_delay == pytest.approx(0.4)


def test_without_the_auto_mode_the_delay_is_the_configured_number_only(tmp_path):
    sync = tmp_path / "sync.json"
    sync.write_text('{"mouth_delay": 2.0}', encoding="utf-8")  # an old value of the auto mode
    spk = Speaker(synth(1), Sink(), RATE, tmp_path / "s.jsonl", mouth_delay=0.4, sync_file=sync)
    assert spk.mouth_delay == 0.4  # the file is not read
    spk._adapt(3.0, 9.0)  # a measured lead does not move it
    assert spk.mouth_delay == 0.4 and sync.read_text(encoding="utf-8") == '{"mouth_delay": 2.0}'


def test_the_same_phrase_is_not_said_twice_within_two_minutes(tmp_path):
    async def scenario():
        clock = [100.0]
        voice = speaker(tmp_path, monotonic=lambda: clock[0])
        phrase = "Поняла, закрываем. Если что-то новое всплывёт — скажи."
        assert await voice.speak(phrase, lambda: None) == "Сказано."
        assert "не повторяю" in await voice.speak(phrase, lambda: None)
        assert await voice.speak("Мм?", lambda: None) == "Сказано."  # short replies may repeat
        assert await voice.speak("Мм?", lambda: None) == "Сказано."
        clock[0] += 130  # long ago: said again
        assert await voice.speak(phrase, lambda: None) == "Сказано."

    asyncio.run(scenario())
