"""Trusted agent gateway (D79/D80), run with the image's Python from stdin configuration.

Two listeners, both bound to addresses the host supplies after the networks exist:

* the model endpoint on the internal network only: OpenAI-compatible chat completions,
  forwarded to the provider with the key that only this process holds; the model name is
  forced, usage is reported to the host, STOP and an exhausted day refuse the call;
* the operator's OpenCode port on the bridge address (published to the host's loopback),
  relayed as plain TCP to the agent container, which itself requires the server password.

Stdout carries JSON rows to the host (never the key, the prompt or the answer). Stdin
carries control rows; its end means the host is gone and the gateway exits.
"""

import http.client
import json
import os
import socket
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 32 * 1024 * 1024
CHUNK = 65536

config = json.loads(sys.stdin.readline())
upstream = urllib.parse.urlsplit(config["upstream"])
KEY = config["key"]
MODEL = config["model"]
TIMEOUT = float(config.get("timeout", 600))
state = {"stopped": False, "remaining": config.get("remaining"), "agent": None}
lock = threading.Lock()
out = threading.Lock()


def emit(row):
    with out:
        sys.stdout.write(json.dumps(row) + "\n")
        sys.stdout.flush()


def control():
    for line in sys.stdin:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        with lock:
            if row.get("kind") == "budget":
                state["remaining"] = row.get("remaining")
            elif row.get("kind") == "agent":
                state["agent"] = row.get("address")
            elif row.get("kind") == "stop":
                state["stopped"] = True
    os._exit(0)


def refusal():
    with lock:
        if state["stopped"]:
            return "STOPPED"
        remaining = state["remaining"]
    if remaining is None:
        return "TARIFF_REQUIRED"
    if remaining <= 0:
        return "BUDGET_EXHAUSTED"
    return None


def usage_of(document, found):
    usage = document.get("usage") if isinstance(document, dict) else None
    if isinstance(usage, dict):
        found.update(usage)


class Model(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, document):
        body = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def refuse(self, code):
        # 403, not 429: the SDK must not retry a refusal the platform decided.
        self.reply(403, {"error": {"message": code, "type": "platform", "code": code}})
        emit({"kind": "refused", "code": code})

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self.reply(200, {"object": "list", "data": [{"id": "bound", "object": "model"}]})
        self.reply(404, {"error": {"message": "NOT_FOUND"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self.reply(404, {"error": {"message": "NOT_FOUND"}})
        size = int(self.headers.get("Content-Length", "-1"))
        if not 0 < size <= MAX_BODY:
            return self.reply(413, {"error": {"message": "BODY_LIMIT"}})
        try:
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except ValueError:
            return self.reply(400, {"error": {"message": "BODY_INVALID"}})
        code = refusal()
        if code is not None:
            return self.refuse(code)
        body["model"] = MODEL
        stream = body.get("stream") is True
        if stream:
            body["stream_options"] = {"include_usage": True}
        data = json.dumps(body).encode()
        found = {}
        status = 0
        try:
            if upstream.scheme == "https":
                connection = http.client.HTTPSConnection(
                    upstream.hostname, upstream.port or 443, timeout=TIMEOUT,
                    context=ssl.create_default_context(),
                )
            else:  # tests only: the host refuses a plain upstream for live use
                connection = http.client.HTTPConnection(
                    upstream.hostname, upstream.port or 80, timeout=TIMEOUT
                )
            connection.request(
                "POST", upstream.path.rstrip("/") + "/chat/completions", data,
                {"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
                 "Accept": "text/event-stream" if stream else "application/json"},
            )
            response = connection.getresponse()
            status = response.status
            self.send_response(status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            pending = b""
            raw = b""
            while True:
                chunk = response.read1(CHUNK) if stream else response.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                if stream:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        line = line.strip()
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                usage_of(json.loads(line[5:]), found)
                            except ValueError:
                                pass
                elif len(raw) <= MAX_BODY:
                    raw += chunk
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            if not stream:
                try:
                    usage_of(json.loads(raw), found)
                except ValueError:
                    pass
            connection.close()
        except (OSError, http.client.HTTPException):
            if status == 0:
                try:
                    self.reply(502, {"error": {"message": "UPSTREAM_UNAVAILABLE"}})
                except OSError:
                    pass
            status = status or 502
        emit({
            "kind": "usage",
            "status": status,
            "input": found.get("prompt_tokens"),
            "output": found.get("completion_tokens"),
            "cached": (found.get("prompt_tokens_details") or {}).get("cached_tokens"),
        })


def pump(source, target):
    try:
        while True:
            data = source.recv(CHUNK)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    for side in (source, target):
        try:
            side.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def relay(server):
    while True:
        client, _ = server.accept()
        with lock:
            agent = None if state["stopped"] else state["agent"]
        if agent is None:
            client.close()
            continue
        try:
            backend = socket.create_connection((agent, config.get("agent_port", 4096)), timeout=10)
            backend.settimeout(None)
        except OSError:
            client.close()
            continue
        threading.Thread(target=pump, args=(client, backend), daemon=True).start()
        threading.Thread(target=pump, args=(backend, client), daemon=True).start()


model = ThreadingHTTPServer((config["model_bind"], config.get("model_port", 8080)), Model)
model.daemon_threads = True
ui = socket.create_server((config["ui_bind"], config.get("ui_port", 4096)))
threading.Thread(target=model.serve_forever, daemon=True).start()
threading.Thread(target=relay, args=(ui,), daemon=True).start()
emit({"kind": "ready"})
control()
