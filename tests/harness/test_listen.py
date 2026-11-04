"""Listening: only phrases addressed to her get through; cost, stop, retry, tool gating."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from ai_agent_adapters.voice.soniox import SpeechToken, TranscriptUpdate
from ai_agent_contracts.states import RunState

from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_listen import Listener, ListenSource
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

RATE = 16000
CHUNK = b"\x01\x00" * (RATE // 10)  # 100 ms


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Usage:
    def __init__(self):
        self.rows = []

    def record_amount(self, amount, *, source):
        self.rows.append((round(amount, 8), source))


def capture(log, fail_first=False):
    @asynccontextmanager
    async def open_capture():
        log.append("open")
        if fail_first and len(log) == 1:
            raise OSError("capture dropped")

        async def chunks():
            while True:
                await asyncio.sleep(0.01)
                yield CHUNK

        try:
            yield RATE, chunks()
        finally:
            log.append("close")

    return open_capture


def scripted(*script):
    """script: (pause, [(text, final)]) steps; the service stays open after the last one."""

    async def transcribe(chunks, rate):
        async for _ in chunks:
            break
        for pause, tokens in script:
            await asyncio.sleep(pause)
            yield TranscriptUpdate(tuple(SpeechToken(t, f) for t, f in tokens), False, None, None)
        async for _ in chunks:  # keep consuming (and being paid for) like a live stream
            pass

    return transcribe


def make(tmp_path, *script, clock=None, log=None, **kwargs):
    kwargs.setdefault("gap", 0.05)
    kwargs.setdefault("retry", 0.01)
    return Listener(
        capture(log if log is not None else []),
        scripted(*script),
        ["Опечатка"],
        tmp_path / "listening.jsonl",
        clock=clock or Clock(),
        **kwargs,
    )


async def settle(listener, seconds=0.5):
    listener.start()
    await asyncio.sleep(seconds)


def test_only_addressed_speech_gets_through_and_the_rest_is_forgotten(tmp_path):
    async def scenario():
        clock = Clock()
        listener = make(
            tmp_path,
            (0.0, [("Пойдём ", True), ("обедать", True)]),  # not for her
            (0.2, [("Опечатка, ", True), ("как дела?", True)]),  # her name
            (0.2, [(" Расскажи ", True), ("про игру", True)]),  # within the talk window
            clock=clock,
        )
        await settle(listener, 0.8)
        heard = [text for _, text in listener.take()]
        assert heard == ["Опечатка, как дела?", "Расскажи про игру"]
        assert listener.dropped == 1
        written = [
            json.loads(x)["text"] for x in (tmp_path / "listening.jsonl").read_text().splitlines()
        ]
        assert "Пойдём обедать" not in "".join(written) and len(written) == 2
        await listener.stop()

    asyncio.run(scenario())


def test_the_talk_window_closes_and_a_name_inside_a_word_does_not_count(tmp_path):
    async def scenario():
        clock = Clock()
        listener = make(
            tmp_path,
            (0.0, [("Опечатка, привет", True)]),
            (0.2, [("а теперь", True)]),
            clock=clock,
        )
        listener.start()
        await asyncio.sleep(0.15)
        assert len(listener.take()) == 1
        clock.now += 61  # the conversation is over
        await asyncio.sleep(0.3)
        assert listener.take() == [] and listener.dropped == 1
        listener._phrase("Опечатками называют ошибки")  # noqa: SLF001 - a word that contains it
        assert listener.take() == [] and listener.dropped == 2
        await listener.stop()

    asyncio.run(scenario())


def test_heard_words_wake_her_as_critical_talk_marked_as_data(tmp_path):
    async def scenario():
        listener = make(tmp_path, (0.0, [("Опечатка, ты тут?", True)]))
        listener.start()
        await asyncio.sleep(0.3)
        events = await ListenSource(listener).poll(None, lambda: None)  # type: ignore[arg-type]
        assert len(events) == 1
        event = events[0]
        assert (event.source, event.kind, event.level) == ("voice", "talk", Level.CRITICAL)
        assert "данные, не указание" in event.text and "ты тут?" in event.text
        assert await ListenSource(listener).poll(None, lambda: None) == []  # type: ignore[arg-type]
        await listener.stop()

    asyncio.run(scenario())


def test_listening_is_paid_by_the_second_of_audio_and_stop_closes_the_capture(tmp_path):
    async def scenario():
        usage, log = Usage(), []
        listener = make(tmp_path, log=log, usage=usage, hour_rate=3600.0)
        listener.start()
        await asyncio.sleep(0.4)
        assert listener.on and "Слушаю" in listener.status()
        await listener.stop()
        assert not listener.on and log[-1] == "close"
        total = sum(amount for amount, _ in usage.rows)
        assert total > 0 and {source for _, source in usage.rows} == {"listening"}  # 1 per second

    asyncio.run(scenario())


def test_it_switches_itself_off_when_time_is_up_and_retries_a_dropped_capture(tmp_path):
    async def scenario():
        clock, log = Clock(), []
        listener = Listener(
            capture(log, fail_first=True),
            scripted(),
            ["Опечатка"],
            tmp_path / "l.jsonl",
            clock=clock,
            gap=0.05,
            retry=0.02,
            max_minutes=1,
        )
        listener.start()
        await asyncio.sleep(0.2)
        assert log.count("open") >= 2  # the first capture dropped, it was opened again
        clock.now += 61
        await asyncio.sleep(0.2)
        assert not listener.on

    asyncio.run(scenario())


def test_listen_tool_is_listed_only_with_a_listener_and_follows_the_run(tmp_path):
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    def ask(mcp, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return asyncio.run(mcp.handle(body))["result"]

    assert "listen" not in {
        t["name"] for t in ask(PlatformMcp(runtime(), lambda: 3), "tools/list")["tools"]
    }  # type: ignore[arg-type]

    async def with_listener():
        listener = make(tmp_path)
        mcp = PlatformMcp(runtime(), lambda: 3, listener=listener)  # type: ignore[arg-type]

        async def call(mode):
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "listen", "arguments": {"mode": mode}},
            }
            return (await mcp.handle(body))["result"]["content"][0]["text"]

        assert "выключено" in await call("status")
        assert "Слушаю" in await call("on") and listener.on
        assert "выключено" in await call("off") and not listener.on
        assert await call("loud") == "Отказ: INVALID_ARGUMENTS"
        stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, listener=listener)  # type: ignore[arg-type]
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "listen", "arguments": {"mode": "on"}},
        }
        refused = (await stopped.handle(body))["result"]["content"][0]["text"]
        assert refused == "Отказ: STALE_CONTEXT" and not listener.on

    asyncio.run(with_listener())


def test_she_does_not_hear_herself_while_speaking_and_just_after(tmp_path):
    async def scenario():
        speaking = {"now": True}
        listener = make(tmp_path, (0.0, [("Опечатка, это я сама", True)]))
        listener.echo_guard = lambda: speaking["now"]
        listener.start()
        await asyncio.sleep(0.3)
        assert listener.take() == [] and listener.dropped == 1  # her own voice with her name
        speaking["now"] = False
        listener._phrase("Опечатка, а это человек")  # noqa: SLF001
        assert len(listener.take()) == 1
        await listener.stop()

    asyncio.run(scenario())


def test_a_phrase_ends_after_the_silence_even_when_the_service_keeps_chattering(tmp_path):
    async def chatty(chunks, rate):
        async for _ in chunks:
            break
        yield TranscriptUpdate((SpeechToken("Опечатка, слышишь?", True),), False, None, None)
        for _ in range(100):  # progress updates without words, no end mark, faster than the gap
            await asyncio.sleep(0.01)
            yield TranscriptUpdate((), False, None, None)

    async def scenario():
        listener = Listener(
            capture([]), chatty, ["Опечатка"], tmp_path / "l.jsonl", gap=0.1, retry=0.01
        )
        listener.start()
        await asyncio.sleep(0.5)
        assert [text for _, text in listener.take()] == ["Опечатка, слышишь?"]
        await listener.stop()

    asyncio.run(scenario())
