"""Soniox wire adapter (doc34), no devices, publication, permission or implicit retries.

Create from the explicitly loaded host environment, never in the agent container. Close the
adapter on STOP and discard downstream playback buffers separately. Closing a generator also
closes its socket. A partially received stream is never reported as a completed recording.
"""

import asyncio
import base64
import binascii
import json
import logging
import math
import os
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

KEY_SETTING = "AI_PLATFORM_SONIOX_API_KEY"
TTS_URL = "wss://tts-rt.soniox.com/tts-websocket"
STT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
# websockets DEBUG logs include handshake headers. Never pass them to the host logger.
WIRE_LOG = logging.Logger("ai_agent_adapters.voice.soniox.wire")
WIRE_LOG.disabled = True
WIRE_LOG.propagate = False


class SonioxError(Exception):
    """Fixed codes and optional HTTP status; no remote message, audio, text, key or URL."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code, self.status = code, status
        super().__init__(code if status is None else f"{code}:{status}")


@dataclass(frozen=True)
class SonioxConfig:
    tts_url: str = TTS_URL
    stt_url: str = STT_URL
    tts_model: str = "tts-rt-v2"
    stt_model: str = "stt-rt-v5"
    sample_rate: int = 24000
    opus_bitrate: int = 32000
    connect_timeout: float = 10
    tts_timeout: float = 120
    stt_timeout: float = 600
    max_audio_bytes: int = 16 * 1024 * 1024
    max_input_bytes: int = 64 * 1024 * 1024
    allow_loopback: bool = False

    def __post_init__(self) -> None:
        for url in (self.tts_url, self.stt_url):
            parsed = urlsplit(url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or not parsed.path
                or (
                    parsed.scheme != "wss"
                    and not (
                        self.allow_loopback
                        and parsed.scheme == "ws"
                        and parsed.hostname in ("127.0.0.1", "::1")
                    )
                )
            ):
                raise SonioxError("SONIOX_ENDPOINT_INVALID")
        if (
            self.sample_rate not in (8000, 16000, 24000, 48000)
            or self.opus_bitrate not in (16000, 32000, 64000, 96000, 128000, 256000)
            or not 0 < self.connect_timeout <= 60
            or not 0 < self.tts_timeout <= 300
            or not 0 < self.stt_timeout <= 18000
            or not 1 <= self.max_audio_bytes <= 128 * 1024 * 1024
            or not 1 <= self.max_input_bytes <= 256 * 1024 * 1024
            or not 1 <= len(self.tts_model) <= 50
            or not 1 <= len(self.stt_model) <= 50
        ):
            raise SonioxError("SONIOX_CONFIG_INVALID")


@dataclass(frozen=True)
class SpeechToken:
    text: str
    is_final: bool
    start_ms: float | None = None
    end_ms: float | None = None
    speaker: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class TranscriptUpdate:
    """Final tokens append; non-final tokens replace the previous non-final hypothesis.

    Endpoint tokens remain tokens, not an automatic instruction to the model. Processed
    milliseconds are NOT a monetary invoice. Token usage is unknown on this wire response.
    """

    tokens: tuple[SpeechToken, ...]
    finished: bool
    final_audio_proc_ms: float | None
    total_audio_proc_ms: float | None


def _frame(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise SonioxError("SONIOX_PROTOCOL_ERROR")
    try:
        body = json.loads(raw)
    except ValueError, UnicodeError:
        raise SonioxError("SONIOX_PROTOCOL_ERROR") from None
    if not isinstance(body, dict):
        raise SonioxError("SONIOX_PROTOCOL_ERROR")
    if "error_code" in body or "error_type" in body:
        status = body.get("error_code")
        raise SonioxError("SONIOX_PROVIDER_ERROR", status if type(status) is int else None)
    return body


def _ms(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise SonioxError("SONIOX_PROTOCOL_ERROR")
    return float(value)


class SonioxAdapter:
    def __init__(self, api_key: SecretStr, config: SonioxConfig | None = None) -> None:
        key = api_key.get_secret_value()
        if not key or len(key) > 250 or any(c.isspace() for c in key):
            raise SonioxError("SONIOX_KEY_INVALID")
        self._key, self.config = api_key, config or SonioxConfig()
        self._connections: set[ClientConnection] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @classmethod
    def from_env(
        cls, config: SonioxConfig | None = None, *, environ: Mapping[str, str] | None = None
    ) -> SonioxAdapter:
        # The host loads its explicit .env. No implicit file search or diagnostic key output.
        values = os.environ if environ is None else environ
        value = values.get(KEY_SETTING)
        if not value:
            raise SonioxError("SONIOX_KEY_MISSING")
        return cls(SecretStr(value), config)

    def _current(self) -> None:
        if self._closed:
            raise SonioxError("SONIOX_CLOSED")

    @asynccontextmanager
    async def _connection(self, url: str) -> AsyncIterator[ClientConnection]:
        self._current()

        async def opening() -> ClientConnection:
            return await connect(
                url,
                additional_headers={"Authorization": "Bearer " + self._key.get_secret_value()},
                open_timeout=self.config.connect_timeout,
                close_timeout=2,
                max_size=1024 * 1024,
                max_queue=4,
                proxy=None,
                logger=WIRE_LOG,
            )

        pending = asyncio.create_task(opening())
        self._tasks.add(pending)
        try:
            ws = await pending
        except WebSocketException, OSError, TimeoutError:
            raise SonioxError("SONIOX_TRANSPORT_ERROR") from None
        finally:
            self._tasks.discard(pending)
        self._connections.add(ws)
        try:
            self._current()
            yield ws
        finally:
            self._connections.discard(ws)
            await ws.close()

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sockets = tuple(self._connections)
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

    async def _tts(
        self, text: str, voice: str, language: str, encoding: str
    ) -> AsyncGenerator[bytes]:
        self._current()
        if (
            not text.strip()
            or len(text) > 5000
            or not 1 <= len(voice) <= 50
            or not 1 <= len(language) <= 50
        ):
            raise SonioxError("SONIOX_INPUT_INVALID")
        sid = uuid4().hex
        total, end, terminal = 0, False, False
        config: dict[str, Any] = {
            "model": self.config.tts_model,
            "voice": voice,
            "language": language,
            "audio_format": encoding,
            "sample_rate": self.config.sample_rate,
            "stream_id": sid,
        }
        if encoding == "opus":
            config["bitrate"] = self.config.opus_bitrate
        try:
            async with asyncio.timeout(self.config.tts_timeout):
                async with self._connection(self.config.tts_url) as ws:
                    await ws.send(json.dumps(config))
                    await ws.send(json.dumps({"stream_id": sid, "text": text, "text_end": True}))
                    for _ in range(10000):
                        body = _frame(await ws.recv())
                        self._current()
                        if body.get("stream_id") != sid:
                            raise SonioxError("SONIOX_PROTOCOL_ERROR")
                        if "audio" in body:
                            if end or not isinstance(body["audio"], str):
                                raise SonioxError("SONIOX_PROTOCOL_ERROR")
                            try:
                                audio = base64.b64decode(body["audio"], validate=True)
                            except ValueError, binascii.Error:
                                raise SonioxError("SONIOX_PROTOCOL_ERROR") from None
                            total += len(audio)
                            if total > self.config.max_audio_bytes:
                                raise SonioxError("SONIOX_AUDIO_LIMIT")
                            self._current()
                            if audio:
                                yield audio
                        if body.get("audio_end") is True:
                            end = True
                        if body.get("terminated") is True:
                            self._current()
                            terminal = True
                            break
                    if not (end and terminal and total):
                        raise SonioxError("SONIOX_INCOMPLETE")
                    self._current()
        except TimeoutError:
            raise SonioxError("SONIOX_TIMEOUT") from None
        except WebSocketException, OSError:
            raise SonioxError("SONIOX_TRANSPORT_ERROR") from None

    async def synthesize_ogg(self, text: str, voice: str, *, language: str = "ru") -> bytes:
        """Complete Opus-in-Ogg recording; never returns a partial file as success."""
        data = bytearray()
        async with aclosing(self._tts(text, voice, language, "opus")) as stream:
            async for chunk in stream:
                data.extend(chunk)
        audio = bytes(data)
        # Do not label raw Opus / a provider error page as a Telegram-compatible Ogg file.
        if not audio.startswith(b"OggS") or b"OpusHead" not in audio[:1024]:
            raise SonioxError("SONIOX_OGG_OPUS_REQUIRED")
        self._current()
        return audio

    async def stream_pcm(
        self, text: str, voice: str, *, language: str = "ru"
    ) -> AsyncGenerator[bytes]:
        """Mono signed 16-bit little-endian PCM at config.sample_rate; provisional until EOF."""
        tail = b""
        async with aclosing(self._tts(text, voice, language, "pcm_s16le")) as stream:
            async for chunk in stream:
                chunk = tail + chunk
                length = len(chunk) // 2 * 2
                if length:
                    self._current()
                    yield chunk[:length]
                tail = chunk[length:]
        if tail:
            raise SonioxError("SONIOX_PCM_ALIGNMENT")
        self._current()

    async def transcribe(
        self,
        audio: AsyncIterable[bytes],
        *,
        sample_rate: int = 16000,
        language_hints: tuple[str, ...] = ("ru",),
    ) -> AsyncGenerator[TranscriptUpdate]:
        """Stream mono s16le input and incremental tokens; does not open a microphone."""
        self._current()
        if (
            sample_rate not in (8000, 16000, 24000, 44100, 48000)
            or not 1 <= len(language_hints) <= 8
            or any(not 1 <= len(s) <= 50 for s in language_hints)
        ):
            raise SonioxError("SONIOX_INPUT_INVALID")
        tasks: set[asyncio.Task[Any]] = set()
        try:
            async with asyncio.timeout(self.config.stt_timeout):
                async with self._connection(self.config.stt_url) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "model": self.config.stt_model,
                                "audio_format": "pcm_s16le",
                                "num_channels": 1,
                                "sample_rate": sample_rate,
                                "language_hints": list(language_hints),
                                "enable_endpoint_detection": True,
                            }
                        )
                    )
                    sender = asyncio.create_task(self._send_audio(ws, audio))
                    heartbeat = asyncio.create_task(self._keepalive(ws))
                    receiver = asyncio.create_task(ws.recv())
                    tasks.update((sender, heartbeat, receiver))
                    self._tasks.update(tasks)
                    waiting: set[asyncio.Task[Any]] = set(tasks)
                    for _ in range(10000):
                        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                        for producer in (sender, heartbeat):
                            if producer in done:
                                # Surface input/network failure, never hang waiting for EOF.
                                producer.result()
                                waiting.discard(producer)
                        if receiver not in done:
                            continue
                        update = _transcript(_frame(receiver.result()))
                        if update.finished and not sender.done():
                            raise SonioxError("SONIOX_INCOMPLETE")
                        self._current()
                        yield update
                        if update.finished:
                            return
                        tasks.discard(receiver)
                        self._tasks.discard(receiver)
                        waiting.discard(receiver)
                        receiver = asyncio.create_task(ws.recv())
                        tasks.add(receiver)
                        self._tasks.add(receiver)
                        waiting.add(receiver)
                    raise SonioxError("SONIOX_EVENT_LIMIT")
        except TimeoutError:
            raise SonioxError("SONIOX_TIMEOUT") from None
        except SonioxError:
            raise
        except WebSocketException, OSError:
            raise SonioxError("SONIOX_TRANSPORT_ERROR") from None
        except Exception:  # noqa: BLE001 - an input iterator may carry private error text
            raise SonioxError("SONIOX_INPUT_OR_PROTOCOL_ERROR") from None
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.difference_update(tasks)

    async def _send_audio(self, ws: ClientConnection, audio: AsyncIterable[bytes]) -> None:
        total = 0
        iterator = aiter(audio)
        try:
            async for chunk in iterator:
                self._current()
                if not isinstance(chunk, bytes) or len(chunk) > 65536 or len(chunk) % 2:
                    raise SonioxError("SONIOX_INPUT_INVALID")
                total += len(chunk)
                if total > self.config.max_input_bytes:
                    raise SonioxError("SONIOX_INPUT_LIMIT")
                if chunk:
                    await ws.send(chunk)
            await ws.send("")  # text EOF; an empty binary frame does NOT finish Soniox STT
        finally:
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                await asyncio.wait_for(closer(), 2)

    async def _keepalive(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(20)
            self._current()
            await ws.send('{"type":"keepalive"}')


def _transcript(body: dict[str, Any]) -> TranscriptUpdate:
    raw = body.get("tokens", [])
    if (
        not isinstance(raw, list)
        or len(raw) > 4096
        or type(body.get("finished", False)) is not bool
    ):
        raise SonioxError("SONIOX_PROTOCOL_ERROR")
    tokens = []
    for token in raw:
        if (
            not isinstance(token, dict)
            or not isinstance(token.get("text"), str)
            or len(token["text"]) > 16000
            or type(token.get("is_final")) is not bool
            or any(
                token.get(k) is not None and not isinstance(token[k], str)
                for k in ("speaker", "language")
            )
        ):
            raise SonioxError("SONIOX_PROTOCOL_ERROR")
        tokens.append(
            SpeechToken(
                token["text"],
                token["is_final"],
                _ms(token.get("start_ms")),
                _ms(token.get("end_ms")),
                token.get("speaker"),
                token.get("language"),
            )
        )
    return TranscriptUpdate(
        tuple(tokens),
        body.get("finished", False),
        _ms(body.get("final_audio_proc_ms")),
        _ms(body.get("total_audio_proc_ms")),
    )
