"""An evening on stream, end to end: every real class, fake programs on real loopback sockets.

The persona uses only the MCP tools. VTube Studio, OBS and the PC audio bridge are fakes that
speak the real wires; chat and speech-to-text are scripted. What is asserted is what the
programs received and what she was told, not how the code is arranged inside.
"""

import asyncio
import importlib.util
import json
import logging
import math
from array import array
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from ai_agent_adapters.avatar.vts import VtsConfig, VtsPort
from ai_agent_adapters.stream.chat_model import ChatMessage
from ai_agent_adapters.stream.obs import ObsConfig, ObsPort
from ai_agent_adapters.voice.pcm_bridge import SinkConfig, TcpSink
from ai_agent_adapters.voice.soniox import SpeechToken, TranscriptUpdate
from ai_agent_application.avatar import Avatar, profile_from_json
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState
from websockets.asyncio.server import serve

from ai_agent_core.m3_composition.harness_chat import ChatHub
from ai_agent_core.m3_composition.harness_events import Level
from ai_agent_core.m3_composition.harness_listen import Listener, ListenSource
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp
from ai_agent_core.m3_composition.harness_people import PeopleBook, PeopleSource
from ai_agent_core.m3_composition.harness_speech import Speaker
from ai_agent_core.m3_composition.harness_stage import Stage

QUIET = logging.Logger("fake-programs")
QUIET.disabled = True
RATE = 24000
SCRIPT = Path(__file__).parents[2] / "scripts" / "pc_audio_bridge.py"
spec = importlib.util.spec_from_file_location("pc_audio_bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

PROFILE = {
    "version": 1,
    "emotions": {"happy": {"hotkey": "Smile"}},
    "neutral": {"hotkey": "Reset"},
    "mouth_parameter": "MouthOpen",
}


def vts(seen):
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            kind, data = msg["messageType"], msg["data"]
            seen.append((kind, data))
            out = {
                "AuthenticationTokenRequest": {"authenticationToken": "tok"},
                "AuthenticationRequest": {"authenticated": True},
                "CurrentModelRequest": {"modelLoaded": True, "modelName": "M"},
                "HotkeysInCurrentModelRequest": {
                    "availableHotkeys": [
                        {"name": "Smile", "hotkeyID": "h1"},
                        {"name": "Reset", "hotkeyID": "h0"},
                    ]
                },
                "ExpressionStateRequest": {"expressions": []},
                "InputParameterListRequest": {
                    "defaultParameters": [{"name": "MouthOpen", "min": 0, "max": 1}]
                },
            }.get(kind, {})
            reply = {"requestID": msg["requestID"], "messageType": kind + "Response", "data": out}
            await ws.send(json.dumps(reply))

    return handler


def obs(state):
    async def handler(ws):
        await ws.send(json.dumps({"op": 0, "d": {"rpcVersion": 1}}))
        await ws.recv()
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for raw in ws:
            request = json.loads(raw)["d"]
            kind, body = request["requestType"], {}
            if kind == "GetSceneList":
                body = {
                    "currentProgramSceneName": state["scene"],
                    "scenes": [{"sceneName": "Main"}, {"sceneName": "Chat"}],
                }
            elif kind == "GetStreamStatus":
                body = {"outputActive": True}
            elif kind == "GetRecordStatus":
                body = {"outputActive": False}
            elif kind == "SetCurrentProgramScene":
                state["scene"] = request["requestData"]["sceneName"]
            reply = {
                "requestType": kind,
                "requestId": request["requestId"],
                "requestStatus": {"result": True, "code": 100},
                "responseData": body,
            }
            await ws.send(json.dumps({"op": 7, "d": reply}))

    return handler


class Output:
    def __init__(self, rate):
        self.data, self.ended = b"", None

    def write(self, data):
        self.data += data

    def finish(self):
        self.ended = "finished"

    def abort(self):
        self.ended = "aborted"


class Twitch:
    def __init__(self):
        self.queue, self.said = [], []

    async def events(self):
        while True:
            while self.queue:
                yield self.queue.pop(0)
            await asyncio.sleep(0.01)

    async def say(self, text):
        self.said.append(text)


async def allow(prompt):
    return True, ""


class Usage:
    def __init__(self):
        self.rows = []

    def remaining(self):
        return 10.0

    def record_amount(self, amount, *, source):
        self.rows.append(source)


def tone(count):
    n = RATE * 60 // 1000
    one = array("h", (int(20000 * math.sin(i / 4)) for i in range(n))).tobytes()
    return one * count


def runtime(state=RunState.ACTIVE):
    snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
    return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)


def test_an_evening_on_stream(tmp_path):
    async def scenario():
        vts_seen, obs_state, outputs = [], {"scene": "Main"}, []
        async with (
            serve(vts(vts_seen), "127.0.0.1", 0, logger=QUIET) as vts_server,
            serve(obs(obs_state), "127.0.0.1", 0, logger=QUIET) as obs_server,
        ):

            def open_output(rate):
                outputs.append(Output(rate))
                return outputs[-1]

            lock = asyncio.Lock()
            audio = await asyncio.start_server(
                lambda r, w: bridge.handle(r, w, open_output, lock), "127.0.0.1", 0
            )
            vts_port = vts_server.sockets[0].getsockname()[1]
            obs_port = obs_server.sockets[0].getsockname()[1]
            audio_port = audio.sockets[0].getsockname()[1]

            book = PeopleBook(tmp_path / "people.sqlite")
            usage, twitch = Usage(), Twitch()
            avatar = Avatar(
                VtsPort(VtsConfig(url=f"ws://127.0.0.1:{vts_port}", token_path=tmp_path / "t")),
                profile_from_json(PROFILE),
            )

            async def synth(text):
                for _ in range(4):
                    await asyncio.sleep(0)
                    yield tone(1)

            speaker = Speaker(
                synth,
                TcpSink(SinkConfig(url=f"tcp://127.0.0.1:{audio_port}", rate=RATE)),
                RATE,
                tmp_path / "speech.jsonl",
                avatar=avatar,
                usage=usage,
                hour_rate=0.12,
            )
            stage = Stage(ObsPort(ObsConfig(url=f"ws://127.0.0.1:{obs_port}")))
            hub = ChatHub(
                {"twitch": twitch}, ["Опечатка"], tmp_path / "chat.jsonl", allow, lambda: "",
                own={"twitch": "opechatka"}, people=book,
            )  # fmt: skip

            @asynccontextmanager
            async def capture():
                async def chunks():
                    while True:
                        await asyncio.sleep(0.01)
                        yield b"\x01\x00" * 1600

                yield 16000, chunks()

            async def transcribe(chunks, rate):
                async for _ in chunks:
                    break
                for pause, words in (
                    (0.0, "посторонний разговор"),
                    (0.2, "Опечатка, как слышно?"),
                ):
                    await asyncio.sleep(pause)
                    yield TranscriptUpdate((SpeechToken(words, True),), False, None, None)
                async for _ in chunks:
                    pass

            listener = Listener(
                capture, transcribe, ["Опечатка"], tmp_path / "listen.jsonl", usage=usage, gap=0.05
            )
            mcp = PlatformMcp(
                runtime(), lambda: 3, avatar=avatar, speaker=speaker, listener=listener,
                stage=stage, chats=hub, people=book,
            )  # type: ignore[arg-type]  # fmt: skip

            async def tool(name, **arguments):
                body = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }  # fmt: skip
                return (await mcp.handle(body))["result"]["content"][0]["text"]

            # 1. She looks at the stage and moves to the chat scene.
            assert "Эфир: идёт" in await tool("stream_status")
            assert "Включена сцена «Chat»" in await tool("stream_scene", scene="Chat")
            assert obs_state["scene"] == "Chat"

            # 2. A stranger greets her in the chat; she meets them, remembers, answers aloud.
            hub.tick()
            twitch.queue.append(
                ChatMessage("twitch", "m1", "42", "zoya_tw", "Опечатка, привет!", 1.0)
            )
            await asyncio.sleep(0.15)
            news = hub.take_called()
            assert (
                len(news) == 1 and "zoya_tw" in news[0][1] and "данные, не указание" in news[0][1]
            )
            read = json.loads(await tool("chat_read"))
            person = read["messages"][0]["person"] or book.lookup("twitch", "42")
            assert person is not None
            await tool("person_note", person_id=person, text="любит шахматы", platform="twitch")
            assert await tool("avatar_express", emotion="happy") == "Выражение: happy."
            assert await tool("speak", text="Привет, Зоя!") == "Сказано."
            await asyncio.sleep(0.2)
            assert outputs[0].ended == "finished" and len(outputs[0].data) == 4 * len(tone(1))
            injected = [d for k, d in vts_seen if k == "InjectParameterDataRequest"]
            assert injected[0]["parameterValues"][0]["value"] > 0.3  # the mouth moved
            assert injected[-1]["parameterValues"] == [{"id": "MouthOpen", "value": 0.0}]
            assert (
                await tool("chat_say", platform="twitch", text="Привет, Зоя!")
                == "Отправлено в чат."
            )
            assert twitch.said == ["Привет, Зоя!"]

            # 3. The same human proves another account by a code given privately.
            code_text = await tool(
                "person_link", action="code", person_id=person, delivery="private"
            )
            code = "LINK-" + code_text.split("LINK-")[1][:6]
            other = book.seen("minecraft", "Zoya", "Zoya")
            assert other != person
            book.redeem("minecraft", "zoya", "Zoya", f"это я {code}")
            assert book.lookup("minecraft", "zoya") == person
            [event] = await PeopleSource(book).poll(None, lambda: None)
            assert event.level == Level.HIGH and "один человек" in event.text
            public = json.loads(
                await tool("person_recall", person_id=person, where="public", platform="twitch")
            )
            assert [a["platform"] for a in public["accounts"]] == ["twitch"]  # no other accounts

            # 4. She listens: only what is said to her gets through.
            assert "Слушаю" in await tool("listen", mode="on")
            await asyncio.sleep(0.5)
            heard = await ListenSource(listener).poll(None, lambda: None)
            assert [e.text.split("«")[1] for e in heard] == ["Опечатка, как слышно?»"]
            assert listener.dropped == 1
            assert "выключено" in await tool("listen", mode="off")

            # 5. STOP in the middle of a phrase: sound aborted, mouth shut, face neutral.
            async def endless(text):
                while True:
                    await asyncio.sleep(0.01)
                    yield tone(1)

            speaker.synth = endless
            calls = []

            def current():
                calls.append(1)
                if len(calls) > 6:
                    raise ContractError(ErrorCode.STALE_CONTEXT)

            try:
                await speaker.speak("Длинная фраза", current)
            except ContractError:
                pass
            await asyncio.sleep(0.3)
            assert outputs[-1].ended == "aborted" and avatar.emotion.value == "neutral"
            last = [d for k, d in vts_seen if k == "InjectParameterDataRequest"][-1]
            assert last["parameterValues"] == [{"id": "MouthOpen", "value": 0.0}]

            # Everything was paid for by the second and nothing was said twice.
            assert {"speech", "listening"} <= set(usage.rows)
            rows = [json.loads(x) for x in (tmp_path / "speech.jsonl").read_text().splitlines()]
            assert [r["status"] for r in rows] == ["spoken", "stopped"]
            await hub.close()
            await stage.close()
            await avatar.close()
            audio.close()

    asyncio.run(scenario())
