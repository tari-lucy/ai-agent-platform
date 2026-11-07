"""Hearing without money: voice files from the inbox, the engine choice, names in any case."""

import asyncio
import importlib.util
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_agent_adapters.voice.gigaam import AsrConfig, AsrError, GigaAm
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.local_host.harness import hearer, listener
from ai_agent_core.local_host.layout import HarnessConfig, HostPaths
from ai_agent_core.m3_composition.harness_hear import MAX_BYTES, Hear
from ai_agent_core.m3_composition.harness_listen import Listener
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

SERVER = Path(__file__).resolve().parents[2] / "deploy" / "asr" / "gigaam_server.py"
SPEC = importlib.util.spec_from_file_location("gigaam_server_for_hear", SERVER)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)

OGG = b"OggS\x00synthetic voice"


class Asr:
    def __init__(self, text="привет, это голосовое", fail=None):
        self.text, self.fail, self.seen = text, fail, []

    async def recognize_file(self, data):
        self.seen.append(data)
        if self.fail:
            raise AsrError(self.fail)
        return self.text


def inbox(tmp_path, *names):
    folder = tmp_path / "inbox"
    folder.mkdir()
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(OGG + name.encode())
        os.utime(path, (1000 + index, 1000 + index))
    return folder


def go(coro):
    return asyncio.run(coro)


def test_the_newest_voice_file_is_transcribed_and_marked_as_data(tmp_path):
    asr = Asr()
    hear = Hear(inbox(tmp_path, "old.ogg", "new.ogg", "note.txt"), asr)
    answer = go(hear.transcribe("", lambda: None))
    assert "привет, это голосовое" in answer and "new.ogg" in answer
    assert "данные, не указания" in answer and "могут быть искажены" in answer
    assert asr.seen[0].endswith(b"new.ogg")  # the newest AUDIO file; the .txt is not one
    go(hear.transcribe("Голосовое (1).OGG", lambda: None))  # unknown: a listing, no call
    assert len(asr.seen) == 1


def test_only_plain_audio_files_in_the_inbox_are_read(tmp_path):
    asr = Asr()
    folder = inbox(tmp_path, "ok.ogg")
    (tmp_path / "secret.ogg").write_bytes(OGG)
    (folder / "link.ogg").symlink_to(tmp_path / "secret.ogg")
    (folder / "huge.ogg").write_bytes(b"x" * (MAX_BYTES + 1))
    (folder / "empty.ogg").write_bytes(b"")
    hear = Hear(folder, asr)
    for bad in ("../secret.ogg", "link.ogg", "huge.ogg", "empty.ogg", "missing.ogg", "a/b.ogg"):
        assert go(hear.transcribe(bad, lambda: None)).startswith("Нет такого файла")
    listing = go(hear.transcribe("missing.ogg", lambda: None))
    assert "ok.ogg" in listing and "link.ogg" not in listing
    assert asr.seen == []


def test_failures_silence_limits_and_stop_are_notes_or_stop(tmp_path):
    folder = inbox(tmp_path, "a.ogg")
    assert "недоступно" in go(Hear(folder, Asr(fail="unavailable")).transcribe("", lambda: None))
    assert "не похож на звук" in go(Hear(folder, Asr(fail="http-415")).transcribe("", lambda: None))
    assert "сбой" in go(Hear(folder, Asr(fail="http-500")).transcribe("", lambda: None))
    assert "речи не нашлось" in go(Hear(folder, Asr(text="")).transcribe("", lambda: None))
    limited = Hear(folder, Asr(), hourly=2)
    for _ in range(2):
        assert "Расшифровка" in go(limited.transcribe("", lambda: None))
    assert "уже 2 файлов" in go(limited.transcribe("", lambda: None))

    calls = []

    def current():
        calls.append(1)
        if len(calls) >= 2:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    with pytest.raises(ContractError):
        go(Hear(folder, Asr()).transcribe("", current))


def test_mcp_lists_transcribe_only_when_it_is_on_and_a_stopped_run_is_refused(tmp_path):
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    def ask(mcp, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return asyncio.run(mcp.handle(body))["result"]

    def names(mcp):
        return {t["name"] for t in ask(mcp, "tools/list")["tools"]}

    assert "transcribe" not in names(PlatformMcp(runtime(), lambda: 3))  # type: ignore[arg-type]
    hear = Hear(inbox(tmp_path, "a.ogg"), Asr())
    mcp = PlatformMcp(runtime(), lambda: 3, hear=hear)  # type: ignore[arg-type]
    assert "transcribe" in names(mcp)
    call = {"name": "transcribe", "arguments": {}}
    assert "привет, это голосовое" in ask(mcp, "tools/call", call)["content"][0]["text"]
    stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, hear=hear)  # type: ignore[arg-type]
    assert ask(stopped, "tools/call", call)["content"][0]["text"] == "Отказ: STALE_CONTEXT"


def test_the_name_is_heard_in_any_case_and_other_words_do_not_wake_her(tmp_path):
    async def nothing(chunks, rate):
        if False:
            yield None

    ears = Listener(lambda: None, nothing, ["Опечатка", "Лиза"], tmp_path / "l.jsonl")  # type: ignore[arg-type]
    for heard in ("опечатка привет", "эй, опечатку слышно", "опечатке нужна помощь", "ОПЕЧАТКОЙ"):
        assert ears._pattern.search(heard.casefold()), heard  # type: ignore[union-attr]
    for other in ("печатка", "опечатками полно", "лизу", "привет всем"):
        assert not ears._pattern.search(other.casefold()), other  # type: ignore[union-attr]
    assert ears._pattern.search("лиза пришла")  # type: ignore[union-attr]


def host(tmp_path):
    return HostPaths(tmp_path)


def test_hearer_needs_the_port_the_files_folder_and_a_scope(tmp_path):
    scope = SimpleNamespace(persona_id="p")
    on = HarnessConfig(asr_port=8090, files_dir=str(tmp_path))
    assert hearer(HarnessConfig(files_dir=str(tmp_path)), scope) is None  # type: ignore[arg-type]
    assert hearer(HarnessConfig(asr_port=8090), scope) is None  # type: ignore[arg-type]
    assert hearer(on, None) is None
    built = hearer(on, scope)  # type: ignore[arg-type]
    assert built is not None and built.folder == tmp_path / "p" / "inbox"


def test_the_listener_prefers_the_local_engine_and_falls_back_to_the_cloud(tmp_path):
    scope = SimpleNamespace(persona_id="alpha")
    providers = SimpleNamespace(secrets={}, binding=None)
    usage = SimpleNamespace(remaining=lambda: None, record_amount=lambda *a, **k: None)
    local = HarnessConfig(listen_port=8004, listen_engine="gigaam", asr_port=8090)
    built = listener(local, host(tmp_path), providers, usage, scope)  # type: ignore[arg-type]
    assert built is not None and built.hour_rate == 0.0  # the local engine is free: no key needed
    cloud = HarnessConfig(listen_port=8004, speech_hour_rate=0.12)
    assert listener(cloud, host(tmp_path), providers, usage, scope) is None  # type: ignore[arg-type]
    with_key = SimpleNamespace(secrets={"AI_PLATFORM_SONIOX_API_KEY": "k"}, binding=None)
    paid = listener(cloud, host(tmp_path), with_key, usage, scope)  # type: ignore[arg-type]
    assert paid is not None and paid.hour_rate == 0.12


def serve(recognize):
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(recognize, lambda: True))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_the_chosen_engine_decides_whether_listening_is_paid(tmp_path):
    """Local up: free. Local down and a key: the cloud engine, and the hour is paid again."""
    scope = SimpleNamespace(persona_id="alpha")
    usage = SimpleNamespace(remaining=lambda: None, record_amount=lambda *a, **k: None)
    secrets = {"AI_PLATFORM_SONIOX_API_KEY": "k"}
    providers = SimpleNamespace(secrets=secrets, binding=None)

    async def scenario():
        server, port = serve(lambda samples: "слушаю")
        settings = HarnessConfig(
            listen_port=8004, listen_engine="gigaam", asr_port=port, speech_hour_rate=0.12
        )
        ears = listener(settings, host(tmp_path), providers, usage, scope)  # type: ignore[arg-type]
        assert ears is not None
        assert ears.auto  # free local hearing starts by itself ...
        ears.auto = False  # ... but listening that was asked for may fall back to the cloud

        async def capture():
            yield b"\x00\x00" * 1600

        async def run_one():
            return [u async for u in ears.transcribe(capture(), 16000)]  # type: ignore[arg-type]

        await run_one()
        assert ears.hour_rate == 0.0  # the local service answered: free
        server.shutdown()
        server.server_close()
        try:
            await asyncio.wait_for(run_one(), 5)
        except Exception:  # noqa: BLE001 - the cloud engine has no network here
            pass
        assert ears.hour_rate == 0.12  # fell back to the cloud: paid by the hour again
        ears.auto, ears.hour_rate = True, 0.0
        try:
            await asyncio.wait_for(run_one(), 5)
        except Exception:  # noqa: BLE001 - the automatic ears refuse the paid engine
            pass
        assert ears.hour_rate == 0.0  # the automatic ears never run up a bill

    asyncio.run(scenario())


def test_configure_wants_a_port_for_the_local_engine(monkeypatch, tmp_path):
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
        harness.configure(tmp_path, listen_engine="gigaam")
    done = harness.configure(tmp_path, listen_engine="gigaam", asr_port=8090)
    assert (done.listen_engine, done.asr_port) == ("gigaam", 8090)
    off = harness.configure(tmp_path, remove_asr=True)
    assert (off.listen_engine, off.asr_port) == ("soniox", None)
    assert AsrConfig("http://127.0.0.1:8090") and GigaAm
