"""The local recognition service: chunks, limits and the two endpoints, with a fake model."""

import importlib.util
import json
import shutil
import threading
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "deploy" / "asr" / "gigaam_server.py"
SPEC = importlib.util.spec_from_file_location("gigaam_server", PATH)
assert SPEC and SPEC.loader
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)

RATE = 16000


def pcm(seconds, level=3000):
    return level.to_bytes(2, "little", signed=True) * int(seconds * RATE)


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, samples):
        self.calls.append(len(samples))
        return f"фраза{len(self.calls)}"


def serve(recognize):
    handler = server_module.make_handler(recognize, lambda: True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def post(url, data):
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_health_and_a_short_phrase():
    fake = Fake()
    server, url = serve(fake)
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as response:
            assert json.loads(response.read()) == {"ok": True, "loaded": True}
        status, answer = post(url + "/recognize", pcm(2))
        assert status == 200 and answer == {"text": "фраза1"}
        assert fake.calls == [2 * RATE]
    finally:
        server.shutdown()


def test_a_long_stretch_is_cut_into_chunks_and_joined():
    fake = Fake()
    text = server_module.transcribe_pcm(fake, pcm(65))  # 30 + 30 + 5 seconds
    assert fake.calls == [30 * RATE, 30 * RATE, 5 * RATE]
    assert text == "фраза1 фраза2 фраза3"


def test_a_click_shorter_than_a_tenth_of_a_second_is_not_recognised():
    fake = Fake()
    assert server_module.transcribe_pcm(fake, pcm(0.05)) == "" and fake.calls == []


def test_limits_and_unknown_paths():
    server, url = serve(Fake())
    try:
        status, _ = post(url + "/recognize", pcm(61))  # over a minute
        assert status == 413
        status, _ = post(url + "/recognize", b"")
        assert status == 413
        status, _ = post(url + "/nothing", pcm(1))
        assert status == 404
    finally:
        server.shutdown()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed to decode files")
def test_an_audio_file_is_decoded_and_a_non_audio_file_is_refused(tmp_path):
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)  # another rate: ffmpeg converts it
        stream.writeframes(b"\x10\x27" * 8000)  # one second
    fake = Fake()
    server, url = serve(fake)
    try:
        status, answer = post(url + "/recognize-file", path.read_bytes())
        assert status == 200 and answer == {"text": "фраза1"} and fake.calls == [RATE]
        status, _ = post(url + "/recognize-file", b"this is not audio at all")
        assert status == 415
    finally:
        server.shutdown()


def test_a_long_file_makes_the_model_reload_and_a_short_one_does_not():
    refreshed = []
    handler = server_module.make_handler(Fake(), lambda: True, lambda: refreshed.append(1))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        post(url + "/recognize", pcm(10))  # a live phrase: the model stays
        assert refreshed == []
        post(url + "/recognize", pcm(55))  # still not long
        assert refreshed == []
        server_module.LONG_SECONDS = 5  # shrink the threshold instead of sending minutes
        post(url + "/recognize", pcm(10))
        assert refreshed == [1]
    finally:
        server_module.LONG_SECONDS = 90
        server.shutdown()
        server.server_close()
