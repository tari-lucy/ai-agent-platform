"""Local speech recognition for the persona's ears: GigaAM v2 (CTC, sherpa-onnx), no money.

Runs in a container made from the operator's own ``transcriber`` image (it already holds
sherpa-onnx, numpy, ffmpeg and the model files in its data volume, which this container only
reads). It answers short requests, one at a time, and keeps the model loaded between them:

    GET  /health            -> {"ok": true, "loaded": false}
    POST /recognize         raw 16-bit little-endian mono PCM at 16 kHz (up to 60 s)
                            -> {"text": "..."}
    POST /recognize-file    an audio file (ogg/opus, mp3, wav, m4a, flac, webm; up to 20 MB and
                            10 minutes), decoded by ffmpeg -> {"text": "..."}

It is published on the host's loopback only (see install-asr.sh); there is no key because
nothing outside the machine can reach it, and nothing it receives is stored.
"""

import ctypes
import gc
import json
import os
import subprocess
import tempfile
import threading
from array import array
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RATE = 16000
LONG_SECONDS = 90  # a file longer than this makes the model be reloaded (its buffers do not shrink)
CHUNK_SECONDS = 30  # a longer stretch is cut: memory stays small, long recordings still work
MAX_PCM = RATE * 2 * 60
MAX_FILE = 20 * 1024 * 1024
MAX_FILE_SECONDS = 600
FFMPEG_TIMEOUT = 180

Recognize = Callable[[list[float]], str]  # 16 kHz samples in -1..1 -> text


def release_memory() -> None:
    """Give freed memory back to the system: a long recording would otherwise leave the process
    holding the memory of its biggest moment (measured: 570 MB idle, 1.1 GB after three minutes)."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):  # fmt: skip
        pass


def samples_of(pcm: bytes) -> list[float]:
    values = array("h")
    values.frombytes(pcm[: len(pcm) // 2 * 2])
    return [v / 32768.0 for v in values]


def transcribe_pcm(recognize: Recognize, pcm: bytes) -> str:
    """The text of a PCM stretch, cut into chunks of CHUNK_SECONDS and joined."""
    size = RATE * 2 * CHUNK_SECONDS
    parts: list[str] = []
    for start in range(0, len(pcm), size):
        chunk = pcm[start : start + size]
        if len(chunk) < RATE // 10 * 2:  # under a tenth of a second: nothing to recognise
            continue
        text = recognize(samples_of(chunk)).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def file_to_pcm(data: bytes) -> bytes:
    """Any audio file as 16 kHz mono PCM, through ffmpeg (the file lives only for the call)."""
    with tempfile.NamedTemporaryFile(prefix="asr-", delete=True) as source:
        source.write(data)
        source.flush()
        done = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                source.name,
                "-t",
                str(MAX_FILE_SECONDS),
                "-vn",
                "-ar",
                str(RATE),
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
    if done.returncode != 0 or not done.stdout:
        raise ValueError("not an audio file")
    return done.stdout


class Engine:
    """The model, loaded on first use; one recognition at a time."""

    def __init__(self, model_dir: str, threads: int) -> None:
        self.model_dir, self.threads = model_dir, threads
        self._recognizer = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def reload(self) -> None:
        """Drop the model and load a fresh one: the engine keeps the memory of its biggest job for
        as long as the model lives (measured: 520 MB idle, 1 GB after three minutes of audio)."""
        with self._lock:
            self._recognizer = None
            release_memory()
        threading.Thread(target=lambda: self([0.0] * (RATE // 5)), daemon=True).start()

    def __call__(self, samples: list[float]) -> str:
        with self._lock:
            if self._recognizer is None:
                import sherpa_onnx  # type: ignore[import-not-found,unused-ignore]

                self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                    model=os.path.join(self.model_dir, "model.onnx"),
                    tokens=os.path.join(self.model_dir, "tokens.txt"),
                    num_threads=self.threads,
                    sample_rate=RATE,
                    feature_dim=64,
                    decoding_method="greedy_search",
                    provider="cpu",
                    debug=False,
                )
            stream = self._recognizer.create_stream()
            stream.accept_waveform(RATE, samples)
            self._recognizer.decode_stream(stream)
            return str(stream.result.text or "")


def make_handler(
    recognize: Recognize,
    loaded: Callable[[], bool],
    refresh: Callable[[], None] = lambda: None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # nothing about what was said is logged
            return None

        def reply(self, status: int, document: dict[str, object]) -> None:
            data = json.dumps(document, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.reply(200, {"ok": True, "loaded": loaded()})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            limit = {"/recognize": MAX_PCM, "/recognize-file": MAX_FILE}.get(self.path)
            if limit is None:
                return self.reply(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            if not 0 < size <= limit:
                remaining = size if 0 < size <= 4 * limit else 0
                while remaining > 0:  # let the client finish sending, then answer (no broken pipe)
                    remaining -= len(self.rfile.read(min(remaining, 65536)) or b"\0" * remaining)
                return self.reply(413, {"error": "body size"})
            body = self.rfile.read(size)
            try:
                pcm = body if self.path == "/recognize" else file_to_pcm(body)
                text = transcribe_pcm(recognize, pcm)
            except ValueError:
                return self.reply(415, {"error": "not an audio file"})
            except (OSError, subprocess.SubprocessError):  # fmt: skip
                return self.reply(500, {"error": "recognition failed"})
            finally:
                del body
                release_memory()
            if len(pcm) > LONG_SECONDS * RATE * 2:
                refresh()  # after the answer is ready; the next request waits for the reload
            self.reply(200, {"text": text})

    return Handler


def main() -> None:
    engine = Engine(
        os.environ.get("MODEL_DIR", "/data/models"), int(os.environ.get("THREADS", "2"))
    )
    port = int(os.environ.get("PORT", "8090"))
    # Load the model now, not on the first phrase (it takes about twenty seconds): /health says
    # "loaded" once it is ready, and nobody has to wait for it in the middle of a conversation.
    threading.Thread(target=lambda: engine([0.0] * (RATE // 5)), daemon=True).start()
    # Bound inside the container; docker publishes the port on the host's loopback only.
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), make_handler(engine, lambda: engine.loaded, engine.reload)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
