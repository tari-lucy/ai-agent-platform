"""Audio bridge for the operator's computer (doc37): plays the persona's speech into a device.

Run next to the virtual cable (VB-Cable). The server reaches this program on 127.0.0.1 through
the reverse tunnel; nothing here listens outside the computer. One connection is one phrase.
Wire format (must match packages/adapters/.../voice/pcm_bridge.py, this file is standalone):

    b"APCM" + u32 rate + u8 channels, then frames (u32 length + 16-bit LE PCM), a zero-length
    frame ends a phrase normally (it is played out, then this program answers one byte, b"\\x06",
    "played"); a connection that closes before that is an abort and the audio is thrown away.

Capture (listening) is the other direction, on its own port: the server connects, this program
sends the header and frames from an INPUT device (for a call: the virtual cable that carries
the other people's voices) until the server closes the connection.

Needs: pip install sounddevice.   List devices:  python pc_audio_bridge.py --list-devices
Run:  python pc_audio_bridge.py --port 8003 --device "CABLE Input" \\
          --capture-port 8004 --capture-device "CABLE Output" --capture-rate 16000
The device is matched by a part of its name, case-insensitively. Stop with Ctrl+C.
"""

import argparse
import asyncio
import struct
import sys
from collections.abc import Callable
from typing import Protocol

HEADER = struct.Struct(">4sIB")
FRAME = struct.Struct(">I")
MAX_FRAME = 256 * 1024
MAX_RATE = 96000


class Output(Protocol):
    def write(self, data: bytes) -> None: ...

    def finish(self) -> None: ...  # play out what is buffered, then close

    def abort(self) -> None: ...  # throw away what is buffered, close


class DeviceOutput:
    """sounddevice output stream: blocking writes happen in a thread, not in the loop."""

    def __init__(self, device: str | int | None, rate: int) -> None:
        import sounddevice  # type: ignore[import-not-found,unused-ignore]

        self.stream = sounddevice.RawOutputStream(
            samplerate=rate, channels=1, dtype="int16", device=device
        )
        self.stream.start()

    def write(self, data: bytes) -> None:
        self.stream.write(data)

    def finish(self) -> None:
        self.stream.stop()  # stop() waits for the buffered audio to play
        self.stream.close()

    def abort(self) -> None:
        try:  # may race with a stop() that is still playing out in another thread
            self.stream.abort()
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass


def find_device(name: str | None, *, capture: bool = False) -> str | int | None:
    if name is None:
        return None
    import sounddevice  # type: ignore[import-not-found,unused-ignore]

    for index, device in enumerate(sounddevice.query_devices()):
        channels = device["max_input_channels" if capture else "max_output_channels"]
        if name.lower() in device["name"].lower() and channels > 0:
            return index
    raise SystemExit(f"No output device with {name!r} in its name. Use --list-devices.")


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    open_output: Callable[[int], Output],
    busy: asyncio.Lock,
) -> None:
    if busy.locked():  # one phrase at a time: the server is told by the close
        writer.close()
        return
    async with busy:
        output: Output | None = None
        try:
            magic, rate, channels = HEADER.unpack(await reader.readexactly(HEADER.size))
            if magic != b"APCM" or not 8000 <= rate <= MAX_RATE or channels != 1:
                return
            opened = await asyncio.to_thread(open_output, rate)
            output = opened
            while True:
                (size,) = FRAME.unpack(await reader.readexactly(FRAME.size))
                if size == 0:
                    # Play out what is buffered. If the server hangs up meanwhile (STOP), the
                    # rest is cut off by the abort in `finally`; otherwise say "played".
                    finishing = asyncio.ensure_future(asyncio.to_thread(opened.finish))
                    finishing.add_done_callback(lambda f: f.cancelled() or f.exception())
                    hangup = asyncio.ensure_future(reader.read(1))
                    await asyncio.wait({finishing, hangup}, return_when=asyncio.FIRST_COMPLETED)
                    if not finishing.done():
                        return  # `finally` aborts the playback that is still running
                    hangup.cancel()
                    output = None
                    writer.write(b"\x06")
                    await writer.drain()
                    return
                if size > MAX_FRAME or size % 2:
                    return
                await asyncio.to_thread(opened.write, await reader.readexactly(size))
        except asyncio.IncompleteReadError, ConnectionError:
            pass  # closed without the end frame: an abort
        finally:
            if output is not None:
                await asyncio.to_thread(output.abort)
            writer.close()


class Capture(Protocol):
    def read(self, frames: int) -> bytes: ...

    def close(self) -> None: ...


class DeviceCapture:
    def __init__(self, device: str | int | None, rate: int) -> None:
        import sounddevice  # type: ignore[import-not-found,unused-ignore]

        self.stream = sounddevice.RawInputStream(
            samplerate=rate, channels=1, dtype="int16", device=device
        )
        self.stream.start()

    def read(self, frames: int) -> bytes:
        data, _overflow = self.stream.read(frames)
        return bytes(data)

    def close(self) -> None:
        self.stream.abort()
        self.stream.close()


async def handle_capture(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    open_capture: Callable[[int], Capture],
    rate: int,
    busy: asyncio.Lock,
) -> None:
    """Send the header and 100 ms frames until the server closes the connection."""
    if busy.locked():
        writer.close()
        return
    async with busy:
        capture: Capture | None = None
        try:
            capture = await asyncio.to_thread(open_capture, rate)
            writer.write(HEADER.pack(b"APCM", rate, 1))
            closed = asyncio.ensure_future(reader.read(1))  # EOF here = the server hung up
            while not closed.done():
                data = await asyncio.to_thread(capture.read, rate // 10)
                writer.write(FRAME.pack(len(data)) + data)
                await writer.drain()
            closed.cancel()
        except ConnectionError, OSError:
            pass
        finally:
            if capture is not None:
                await asyncio.to_thread(capture.close)
            writer.close()


async def serve(
    port: int,
    open_output: Callable[[int], Output],
    capture: tuple[int, Callable[[int], Capture], int] | None = None,
) -> None:
    busy = asyncio.Lock()
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, open_output, busy), "127.0.0.1", port
    )
    print(f"Audio bridge on 127.0.0.1:{port}. Ctrl+C to stop.")
    listening = None
    if capture is not None:
        capture_port, open_capture, rate = capture
        lock = asyncio.Lock()
        listening = await asyncio.start_server(
            lambda r, w: handle_capture(r, w, open_capture, rate, lock), "127.0.0.1", capture_port
        )
        print(f"Capture on 127.0.0.1:{capture_port}.")
    async with server:
        if listening is not None:
            async with listening:
                await asyncio.Event().wait()
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--device", help="part of the output device name, e.g. 'CABLE Input'")
    parser.add_argument("--capture-port", type=int, help="listening: port to serve captured audio")
    parser.add_argument(
        "--capture-device", help="part of the INPUT device name, e.g. 'CABLE Output'"
    )
    parser.add_argument("--capture-rate", type=int, default=16000)
    parser.add_argument("--list-devices", action="store_true")
    arguments = parser.parse_args()
    if arguments.list_devices:
        import sounddevice  # type: ignore[import-not-found,unused-ignore]

        print(sounddevice.query_devices())
        return
    if not 1024 <= arguments.port <= 65535:
        sys.exit("Port must be 1024..65535.")
    device = find_device(arguments.device)
    capture = None
    if arguments.capture_port is not None:
        if arguments.capture_rate not in (8000, 16000, 24000, 44100, 48000):
            sys.exit("Capture rate must be 8000, 16000, 24000, 44100 or 48000.")
        in_device = find_device(arguments.capture_device, capture=True)
        capture = (
            arguments.capture_port,
            lambda rate: DeviceCapture(in_device, rate),
            arguments.capture_rate,
        )
    try:
        asyncio.run(serve(arguments.port, lambda rate: DeviceOutput(device, rate), capture))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
