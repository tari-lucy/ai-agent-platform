"""Real loopback WebSockets, fake speech bytes and key; never contacts Soniox."""

import asyncio
import base64
import json
import logging
from dataclasses import replace

import pytest
from ai_agent_adapters.voice.soniox import (
    KEY_SETTING,
    SonioxAdapter,
    SonioxConfig,
    SonioxError,
)
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import serve

KEY = "synthetic-soniox-key"
TEXT = "Синтетическая фраза."
OGG = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"\x00" * 32  # signatures, NOT a playable fixture
QUIET = logging.Logger("fake-soniox-server")
QUIET.disabled = True


def configuration(server, **kwargs):
    port = server.sockets[0].getsockname()[1]
    return SonioxConfig(
        tts_url=f"ws://127.0.0.1:{port}/tts",
        stt_url=f"ws://127.0.0.1:{port}/stt",
        allow_loopback=True,
        **kwargs,
    )


def adapter(config):
    return SonioxAdapter.from_env(config, environ={KEY_SETTING: KEY})


async def tts_start(ws):
    assert ws.request.headers["Authorization"] == "Bearer " + KEY
    config = json.loads(await ws.recv())
    text = json.loads(await ws.recv())
    assert config["language"] == "ru" and config["voice"] == "Maya"
    assert text == {"stream_id": config["stream_id"], "text": TEXT, "text_end": True}
    assert KEY not in json.dumps([config, text])
    return config


async def finish_tts(ws, sid, data):
    await ws.send(
        json.dumps({"stream_id": sid, "audio": base64.b64encode(data).decode(), "audio_end": True})
    )
    await ws.send(json.dumps({"stream_id": sid, "terminated": True}))


def test_ogg_and_pcm_wait_for_terminal_and_preserve_sample_alignment(caplog):
    async def scenario():
        terminal = asyncio.Event()
        audio_sent = asyncio.Event()

        async def handler(ws):
            config = await tts_start(ws)
            sid = config["stream_id"]
            if config["audio_format"] == "opus":
                await ws.send(
                    json.dumps(
                        {
                            "stream_id": sid,
                            "audio": base64.b64encode(OGG).decode(),
                            "audio_end": True,
                        }
                    )
                )
                audio_sent.set()
                await terminal.wait()
                await ws.send(json.dumps({"stream_id": sid, "terminated": True}))
            else:
                assert config["audio_format"] == "pcm_s16le" and config["sample_rate"] == 24000
                await ws.send(json.dumps({"stream_id": sid, "audio": "AQID"}))
                await finish_tts(ws, sid, b"\x04\x05\x06")

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))
            task = asyncio.create_task(client.synthesize_ogg(TEXT, "Maya"))
            await asyncio.wait_for(audio_sent.wait(), 2)
            assert not task.done()  # audio_end alone is not completion
            terminal.set()
            assert await asyncio.wait_for(task, 2) == OGG
            chunks = [chunk async for chunk in client.stream_pcm(TEXT, "Maya")]
            assert all(len(c) % 2 == 0 for c in chunks)
            assert b"".join(chunks) == b"\x01\x02\x03\x04\x05\x06"
            await client.aclose()
            assert not client._connections

    caplog.set_level(logging.DEBUG)
    asyncio.run(scenario())
    assert KEY not in caplog.text and TEXT not in caplog.text


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("provider", "SONIOX_PROVIDER_ERROR"),
        ("wrong_stream", "SONIOX_PROTOCOL_ERROR"),
        ("invalid_base64", "SONIOX_PROTOCOL_ERROR"),
        ("no_audio_end", "SONIOX_INCOMPLETE"),
        ("not_ogg", "SONIOX_OGG_OPUS_REQUIRED"),
        ("limit", "SONIOX_AUDIO_LIMIT"),
    ],
)
def test_tts_failures_never_return_a_completed_file_or_echo_remote_text(failure, expected, caplog):
    async def scenario():
        calls = 0

        async def handler(ws):
            nonlocal calls
            calls += 1
            config = await tts_start(ws)
            sid = config["stream_id"]
            if failure == "provider":
                await ws.send(
                    json.dumps({"stream_id": sid, "error_code": 402, "error_message": KEY + TEXT})
                )
            elif failure == "wrong_stream":
                await ws.send(json.dumps({"stream_id": "other", "terminated": True}))
            elif failure == "invalid_base64":
                await ws.send(json.dumps({"stream_id": sid, "audio": "!bad!"}))
            elif failure == "no_audio_end":
                await ws.send(json.dumps({"stream_id": sid, "terminated": True}))
            else:
                await finish_tts(ws, sid, b"not-ogg" if failure == "not_ogg" else OGG)

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            config = configuration(server, max_audio_bytes=4 if failure == "limit" else 1024)
            client = adapter(config)
            with pytest.raises(SonioxError) as info:
                await client.synthesize_ogg(TEXT, "Maya")
            assert info.value.code == expected
            assert KEY not in str(info.value) and TEXT not in str(info.value)
            assert calls == 1  # no implicit paid retry
            assert not client._connections
            await client.aclose()

    asyncio.run(scenario())
    assert KEY not in caplog.text


def test_stt_binary_audio_text_eof_and_incremental_finality():
    async def scenario():
        async def handler(ws):
            assert ws.request.headers["Authorization"] == "Bearer " + KEY
            config = json.loads(await ws.recv())
            assert config["audio_format"] == "pcm_s16le"
            assert config["sample_rate"] == 16000 and config["num_channels"] == 1
            assert "api_key" not in config
            assert await ws.recv() == b"\x00\x00"
            await ws.send(json.dumps({"tokens": [{"text": "При", "is_final": False}]}))
            assert await ws.recv() == b"\x01\x00"
            await ws.send(
                json.dumps(
                    {
                        "tokens": [
                            {
                                "text": "Привет",
                                "is_final": True,
                                "speaker": "1",
                                "start_ms": 0,
                                "end_ms": 50,
                            }
                        ]
                    }
                )
            )
            assert await ws.recv() == ""  # NOT b''
            await ws.send(
                json.dumps(
                    {
                        "tokens": [],
                        "finished": True,
                        "final_audio_proc_ms": 50,
                        "total_audio_proc_ms": 50,
                    }
                )
            )

        async def audio():
            yield b"\x00\x00"
            yield b"\x01\x00"

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))
            events = [event async for event in client.transcribe(audio())]
            assert events[0].tokens[0].is_final is False
            assert events[1].tokens[0].text == "Привет" and events[1].tokens[0].is_final
            assert events[-1].finished and events[-1].final_audio_proc_ms == 50
            assert not client._connections and not client._tasks
            await client.aclose()

    asyncio.run(scenario())


def test_stop_cancels_stt_producer_and_closes_connection_without_success():
    async def scenario():
        connected, producer_closed, socket_closed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def handler(ws):
            await ws.recv()
            connected.set()
            await ws.wait_closed()
            socket_closed.set()

        async def audio():
            try:
                await asyncio.Event().wait()
                yield b"\x00\x00"
            finally:
                producer_closed.set()

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))

            async def listen():
                return [event async for event in client.transcribe(audio())]

            task = asyncio.create_task(listen())
            await asyncio.wait_for(connected.wait(), 2)
            await client.aclose()
            with pytest.raises((asyncio.CancelledError, SonioxError)):
                await asyncio.wait_for(task, 2)
            await asyncio.wait_for(producer_closed.wait(), 2)
            await asyncio.wait_for(socket_closed.wait(), 2)
            assert not client._tasks and not client._connections

    asyncio.run(scenario())


def test_input_failure_and_truncated_stt_are_not_finished_success(caplog):
    async def scenario():
        async def handler(ws):
            await ws.recv()
            await ws.wait_closed()

        async def audio():
            raise ValueError(KEY + TEXT)
            yield b"\x00\x00"

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))
            with pytest.raises(SonioxError) as info:
                _ = [event async for event in client.transcribe(audio())]
            assert KEY not in str(info.value) and TEXT not in str(info.value)
            assert not client._tasks and not client._connections
            await client.aclose()

    asyncio.run(scenario())
    assert KEY not in caplog.text


def test_env_and_configuration_do_not_read_a_key_at_import_or_accept_insecure_remote_urls():
    with pytest.raises(SonioxError, match="SONIOX_KEY_MISSING"):
        SonioxAdapter.from_env(environ={})
    client = adapter(SonioxConfig())
    assert KEY not in repr(client) and KEY not in repr(client._key)
    for url in (
        "ws://example.com/speech",
        "wss://user:password@example.com/ws",
        "wss://example.com/ws?api_key=secret",
    ):
        with pytest.raises(SonioxError):
            replace(SonioxConfig(), tts_url=url)
    with pytest.raises(SonioxError):
        replace(SonioxConfig(), sample_rate=0)


def test_buffered_tts_terminal_cannot_report_success_after_stop(monkeypatch):
    async def scenario():
        async def handler(ws):
            config = await tts_start(ws)
            await finish_tts(ws, config["stream_id"], OGG)

        original = ClientConnection.recv
        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))

            async def receive(ws, *args, **kwargs):
                raw = await original(ws, *args, **kwargs)
                if isinstance(raw, str) and json.loads(raw).get("terminated"):
                    await client.aclose()
                return raw

            monkeypatch.setattr(ClientConnection, "recv", receive)
            with pytest.raises(SonioxError, match="SONIOX_CLOSED"):
                await client.synthesize_ogg(TEXT, "Maya")
            assert not client._connections

    asyncio.run(scenario())


def test_stop_cancels_in_progress_handshake(monkeypatch):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def connecting(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr("ai_agent_adapters.voice.soniox.connect", connecting)
        client = adapter(SonioxConfig())
        task = asyncio.create_task(client.synthesize_ogg(TEXT, "Maya"))
        await asyncio.wait_for(entered.wait(), 2)
        await client.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and not client._tasks and not client._connections

    asyncio.run(scenario())


def test_cancelled_sender_closes_a_suspended_input_iterator():
    async def scenario():
        sending, closed = asyncio.Event(), asyncio.Event()

        class Socket:
            async def send(self, chunk):
                sending.set()
                await asyncio.Event().wait()

        async def audio():
            try:
                yield b"\x00\x00"
            finally:
                closed.set()

        stream = audio()  # keep a reference: cannot rely on async-generator GC
        client = adapter(SonioxConfig())
        task = asyncio.create_task(client._send_audio(Socket(), stream))
        await asyncio.wait_for(sending.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        await client.aclose()

    asyncio.run(scenario())


def test_stt_disconnect_without_finished_is_not_success():
    async def scenario():
        async def handler(ws):
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps({"tokens": [{"text": "Черновик", "is_final": False}]}))

        async def audio():
            yield b"\x00\x00"

        async with serve(handler, "127.0.0.1", 0, logger=QUIET) as server:
            client = adapter(configuration(server))
            with pytest.raises(SonioxError):
                _ = [update async for update in client.transcribe(audio())]
            await client.aclose()
            assert not client._connections and not client._tasks

    asyncio.run(scenario())
