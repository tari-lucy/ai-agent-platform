"""Audio bridge for the operator's computer (doc37): plays the persona's speech into a device.

Run next to the virtual cable (VB-Cable). The server reaches this program on 127.0.0.1 through
the reverse tunnel; nothing here listens outside the computer. One connection is one phrase.
Wire format (must match packages/adapters/.../voice/pcm_bridge.py, this file is standalone):

    b"APCM" + u32 rate + u8 channels, then frames (u32 length + 16-bit LE PCM), a zero-length
    frame ends a phrase normally (it is played out); a connection that closes without it is an
    abort and the buffered audio is thrown away.

Needs: pip install sounddevice.   List devices:  python pc_audio_bridge.py --list-devices
Run:  python pc_audio_bridge.py --port 8003 --device "CABLE Input"
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
        self.stream.abort()
        self.stream.close()


def find_device(name: str | None) -> str | int | None:
    if name is None:
        return None
    import sounddevice  # type: ignore[import-not-found,unused-ignore]

    for index, device in enumerate(sounddevice.query_devices()):
        if name.lower() in device["name"].lower() and device["max_output_channels"] > 0:
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
                    await asyncio.to_thread(opened.finish)
                    output = None
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


async def serve(port: int, open_output: Callable[[int], Output]) -> None:
    busy = asyncio.Lock()
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, open_output, busy), "127.0.0.1", port
    )
    print(f"Audio bridge on 127.0.0.1:{port}. Ctrl+C to stop.")
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--device", help="part of the output device name, e.g. 'CABLE Input'")
    parser.add_argument("--list-devices", action="store_true")
    arguments = parser.parse_args()
    if arguments.list_devices:
        import sounddevice  # type: ignore[import-not-found,unused-ignore]

        print(sounddevice.query_devices())
        return
    if not 1024 <= arguments.port <= 65535:
        sys.exit("Port must be 1024..65535.")
    device = find_device(arguments.device)
    try:
        asyncio.run(serve(arguments.port, lambda rate: DeviceOutput(device, rate)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
