"""The trusted agent gateway script (D80): key, forced model, usage, refusals, UI relay."""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ai_agent_adapters.sandbox.agent_gateway import GATEWAY

KEY = "synthetic-key-never-echoed"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Upstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.headers["Authorization"], body))
                if body.get("stream"):
                    events = [
                        {"choices": [{"index": 0, "delta": {"content": "При"}}]},
                        {"choices": [{"index": 0, "delta": {"content": "вет"}}]},
                        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7}},
                    ]
                    data = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
                    data = (data + "data: [DONE]\n\n").encode()
                    kind = "text/event-stream"
                else:
                    data = json.dumps(
                        {
                            "choices": [{"message": {"role": "assistant", "content": "Да"}}],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 2},
                        }
                    ).encode()
                    kind = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"


class Echo:
    """A stand-in for the agent's OpenCode server: echoes whatever arrives."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            client, _ = self.server.accept()
            threading.Thread(target=self._echo, args=(client,), daemon=True).start()

    @staticmethod
    def _echo(client: socket.socket) -> None:
        with client:
            while data := client.recv(4096):
                client.sendall(data)


class Gateway:
    def __init__(self, upstream: Upstream, agent: Echo, remaining: float | None = 100.0):
        self.model_port, self.ui_port = free_port(), free_port()
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", GATEWAY],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.rows: list[dict] = []
        self.raw: list[str] = []
        self.send(
            {
                "upstream": upstream.url,
                "key": KEY,
                "model": "provider-model-x",
                "remaining": remaining,
                "model_bind": "127.0.0.1",
                "ui_bind": "127.0.0.1",
                "model_port": self.model_port,
                "ui_port": self.ui_port,
                "agent_port": agent.port,
            }
        )
        assert self.process.stdout is not None
        assert json.loads(self.process.stdout.readline()) == {"kind": "ready"}
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"kind": "agent", "address": "127.0.0.1"})

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.raw.append(line)
            self.rows.append(json.loads(line))

    def send(self, row: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(row) + "\n")
        self.process.stdin.flush()

    def post(self, body: dict) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.model_port}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer marker"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def wait_rows(self, count: int) -> list[dict]:
        deadline = time.monotonic() + 10
        while len(self.rows) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.rows

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


def test_a_streamed_call_carries_the_key_and_forced_model_and_reports_usage_only():
    upstream, agent = Upstream(), Echo()
    gateway = Gateway(upstream, agent)
    try:
        status, body = gateway.post(
            {"model": "anything", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
        )
        assert status == 200 and b"\\u041f" in body and b"[DONE]" in body
        authorization, sent = upstream.calls[0]
        assert authorization == "Bearer " + KEY
        assert sent["model"] == "provider-model-x"
        assert sent["stream_options"] == {"include_usage": True}
        rows = gateway.wait_rows(1)
        assert rows[0] == {
            "kind": "usage",
            "status": 200,
            "input": 120,
            "output": 7,
            "cached": None,
        }
        status, _ = gateway.post({"messages": [{"role": "user", "content": "Hi"}]})
        assert status == 200 and gateway.wait_rows(2)[1]["input"] == 50
        assert not any(KEY in line or "Hi" in line for line in gateway.raw)
    finally:
        gateway.close()


def test_stop_exhausted_budget_and_missing_tariff_refuse_without_a_provider_call():
    upstream, agent = Upstream(), Echo()
    gateway = Gateway(upstream, agent, remaining=None)
    try:
        status, body = gateway.post({"messages": []})
        assert status == 403 and b"TARIFF_REQUIRED" in body
        gateway.send({"kind": "budget", "remaining": 0})
        time.sleep(0.2)
        status, body = gateway.post({"messages": []})
        assert status == 403 and b"BUDGET_EXHAUSTED" in body
        gateway.send({"kind": "budget", "remaining": 5.0})
        gateway.send({"kind": "stop"})
        time.sleep(0.2)
        status, body = gateway.post({"messages": []})
        assert status == 403 and b"STOPPED" in body
        assert upstream.calls == []
    finally:
        gateway.close()


def test_the_ui_port_relays_to_the_agent_until_stop_and_host_loss_ends_the_gateway():
    upstream, agent = Upstream(), Echo()
    gateway = Gateway(upstream, agent)
    try:
        with socket.create_connection(("127.0.0.1", gateway.ui_port), timeout=5) as client:
            client.sendall(b"GET / HTTP/1.1\r\n\r\n")
            assert client.recv(64) == b"GET / HTTP/1.1\r\n\r\n"
        gateway.send({"kind": "stop"})
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", gateway.ui_port), timeout=5) as client:
            assert client.recv(64) == b""  # refused after STOP
        assert gateway.process.stdin is not None
        gateway.process.stdin.close()
        assert gateway.process.wait(timeout=5) == 0
    finally:
        gateway.close()


def test_an_mcp_message_is_relayed_to_the_host_and_its_answer_back_until_stop():
    upstream, agent = Upstream(), Echo()
    gateway = Gateway(upstream, agent)

    def host() -> None:  # the platform side: answer every relayed message
        seen = 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = [r for r in gateway.rows if r.get("kind") == "mcp"]
            for row in rows[seen:]:
                body = row["body"]
                reply = (
                    None
                    if "id" not in body
                    else {"jsonrpc": "2.0", "id": body["id"], "result": {"echo": body["method"]}}
                )
                gateway.send({"kind": "mcp_reply", "id": row["id"], "body": reply})
            seen = len(rows)
            time.sleep(0.02)

    threading.Thread(target=host, daemon=True).start()

    def call(body: dict) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{gateway.model_port}/mcp",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        status, body = call({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        assert status == 200 and json.loads(body) == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"echo": "tools/list"},
        }
        status, body = call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert status == 202 and body == b""
        gateway.send({"kind": "stop"})
        time.sleep(0.2)
        status, body = call({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
        assert status == 403 and b"STOPPED" in body
    finally:
        gateway.close()
