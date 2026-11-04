"""The audio bridge: server-side sink against the real PC script, on a loopback socket."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from ai_agent_adapters.voice.pcm_bridge import (
    BridgeError,
    SinkConfig,
    TcpSink,
    encode_frame,
    encode_header,
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "pc_audio_bridge.py"
spec = importlib.util.spec_from_file_location("pc_audio_bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class Output:
    def __init__(self, rate):
        self.rate, self.data, self.ended = rate, b"", None

    def write(self, data):
        self.data += data

    def finish(self):
        self.ended = "finished"

    def abort(self):
        self.ended = "aborted"


async def pc(outputs):
    def open_output(rate):
        outputs.append(Output(rate))
        return outputs[-1]

    busy = asyncio.Lock()
    server = await asyncio.start_server(
        lambda r, w: bridge.handle(r, w, open_output, busy), "127.0.0.1", 0
    )
    return server, f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def chunks(*parts, pause=0.0):
    for part in parts:
        await asyncio.sleep(pause)
        yield part


def test_a_phrase_is_played_out_in_full_and_finished():
    async def scenario():
        outputs = []
        server, url = await pc(outputs)
        async with server:
            await TcpSink(SinkConfig(url=url)).play(chunks(b"\x01\x00" * 100, b"\x02\x00" * 50))
            await asyncio.sleep(0.2)
        assert outputs[0].rate == 24000 and outputs[0].ended == "finished"
        assert outputs[0].data == b"\x01\x00" * 100 + b"\x02\x00" * 50

    asyncio.run(scenario())


def test_cancelling_the_phrase_aborts_it_on_the_computer():
    async def scenario():
        outputs = []
        server, url = await pc(outputs)
        async with server:
            task = asyncio.create_task(
                TcpSink(SinkConfig(url=url)).play(chunks(*[b"\x01\x00" * 100] * 50, pause=0.05))
            )
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.3)
        assert outputs[0].ended == "aborted" and 0 < len(outputs[0].data) < 100 * 2 * 50

    asyncio.run(scenario())


def test_a_second_phrase_while_playing_is_refused_not_mixed():
    async def scenario():
        outputs = []
        server, url = await pc(outputs)
        async with server:
            first = asyncio.create_task(
                TcpSink(SinkConfig(url=url)).play(
                    chunks(b"\x01\x00" * 10, b"\x01\x00" * 10, pause=0.3)
                )
            )
            await asyncio.sleep(0.1)
            try:
                await TcpSink(SinkConfig(url=url)).play(chunks(b"\x09\x00" * 10, b"\x09\x00" * 10))
            except BridgeError:
                pass
            await first
        assert len(outputs) == 1 and b"\x09" not in outputs[0].data

    asyncio.run(scenario())


def test_wire_rules_and_errors_are_fixed_codes():
    async def scenario():
        with pytest.raises(BridgeError) as error:
            await TcpSink(SinkConfig(url="tcp://127.0.0.1:1", connect_timeout=1)).play(chunks())
        assert error.value.code == "bridge:connect"

    asyncio.run(scenario())
    with pytest.raises(ValueError):
        SinkConfig(url="tcp://203.0.113.5:8003")
    with pytest.raises(ValueError):
        SinkConfig(url="http://127.0.0.1:8003")
    with pytest.raises(BridgeError):
        encode_frame(b"\x00")  # odd byte count is not 16-bit PCM
    with pytest.raises(BridgeError):
        encode_header(100)


class FakeCapture:
    def __init__(self):
        self.closed, self.reads = False, 0

    def read(self, frames):
        import time

        self.reads += 1
        time.sleep(0.01)
        return b"\x03\x00" * frames

    def close(self):
        self.closed = True


def test_capture_sends_frames_until_the_server_hangs_up():
    from ai_agent_adapters.voice.pcm_bridge import TcpSource

    async def scenario():
        devices = []

        def open_capture(rate):
            devices.append(FakeCapture())
            return devices[-1]

        lock = asyncio.Lock()
        server = await asyncio.start_server(
            lambda r, w: bridge.handle_capture(r, w, open_capture, 16000, lock), "127.0.0.1", 0
        )
        url = f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with server:
            got = []
            async with TcpSource(SinkConfig(url=url)).open() as (rate, frames):
                assert rate == 16000
                async for frame in frames:
                    got.append(frame)
                    if len(got) == 3:
                        break
            await asyncio.sleep(0.3)
        assert all(f == b"\x03\x00" * 1600 for f in got)
        assert devices[0].closed  # leaving the context stopped the capture

        with pytest.raises(BridgeError) as error:
            async with TcpSource(SinkConfig(url="tcp://127.0.0.1:1", connect_timeout=1)).open():
                pass
        assert error.value.code == "bridge:connect"

    asyncio.run(scenario())
