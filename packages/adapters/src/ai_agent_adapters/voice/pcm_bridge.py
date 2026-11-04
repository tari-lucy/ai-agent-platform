"""Audio bridge wire (doc37): live PCM to, and from, the operator's computer. Transport only.

The computer runs ``scripts/pc_audio_bridge.py`` next to the virtual cable; the server reaches
it on loopback through the reverse tunnel. A connection is one phrase (or one listening
session):

    header   b"APCM" + u32 sample rate + u8 channels            (9 bytes, big endian)
    frames   u32 length + that many bytes of signed 16-bit little-endian PCM
    end      a frame of length 0: play what is buffered, then stop (a normal ending)

A connection that closes without the end frame is an abort: the other side discards whatever
it still holds (STOP, an interrupted phrase). Nothing is buffered without a bound and no
remote text reaches the caller: errors are fixed codes.
"""

import asyncio
import struct
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

MAGIC = b"APCM"
HEADER = struct.Struct(">4sIB")
FRAME = struct.Struct(">I")
MAX_FRAME = 256 * 1024
MAX_RATE = 96000


class BridgeError(Exception):
    """Fixed code only: ``bridge:<what>``."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def encode_header(rate: int, channels: int = 1) -> bytes:
    if not 8000 <= rate <= MAX_RATE or channels != 1:
        raise BridgeError("bridge:format")
    return HEADER.pack(MAGIC, rate, channels)


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME or len(payload) % 2:
        raise BridgeError("bridge:frame")
    return FRAME.pack(len(payload)) + payload


END = FRAME.pack(0)


async def read_header(reader: asyncio.StreamReader) -> int:
    """The sample rate announced by a connection; the format is checked."""
    try:
        magic, rate, channels = HEADER.unpack(await reader.readexactly(HEADER.size))
    except asyncio.IncompleteReadError:
        raise BridgeError("bridge:closed") from None
    if magic != MAGIC or not 8000 <= rate <= MAX_RATE or channels != 1:
        raise BridgeError("bridge:format")
    return int(rate)


async def read_frames(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Frames until the end frame. A close without it raises ``bridge:aborted``."""
    while True:
        try:
            size: int = FRAME.unpack(await reader.readexactly(FRAME.size))[0]
            if size == 0:
                return
            if size > MAX_FRAME or size % 2:
                raise BridgeError("bridge:frame")
            yield await reader.readexactly(size)
        except asyncio.IncompleteReadError:
            raise BridgeError("bridge:aborted") from None


@dataclass(frozen=True)
class SinkConfig:
    url: str = "tcp://127.0.0.1:8003"
    connect_timeout: float = 5.0
    write_timeout: float = 10.0
    rate: int = 24000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme != "tcp" or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("bridge url must be tcp on loopback (the tunnel puts it there)")
        if parsed.port is None:
            raise ValueError("bridge url needs a port")


class TcpSink:
    """Plays a PCM stream on the computer. Cancelling aborts the phrase there at once."""

    def __init__(self, config: SinkConfig) -> None:
        self.config = config

    async def play(self, chunks: AsyncIterable[bytes]) -> None:
        parsed = urlsplit(self.config.url)
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        except OSError, TimeoutError:
            raise BridgeError("bridge:connect") from None
        del reader
        try:
            writer.write(encode_header(self.config.rate))
            async for chunk in chunks:
                for start in range(0, len(chunk), MAX_FRAME):
                    writer.write(encode_frame(chunk[start : start + MAX_FRAME]))
                async with asyncio.timeout(self.config.write_timeout):
                    await writer.drain()
            writer.write(END)
            async with asyncio.timeout(self.config.write_timeout):
                await writer.drain()
            writer.write_eof()
        except TimeoutError:
            raise BridgeError("bridge:timeout") from None
        except OSError:
            raise BridgeError("bridge:transport") from None
        finally:
            # Closing without END (an exception or a cancel) is the abort signal.
            writer.close()
