"""Cold OpenCode startup, fake in-container HTTP only; no host mounts or paid provider."""

import asyncio
import json
import os
import time
from dataclasses import replace

import pytest
from ai_agent_application.agent_workplace.ports import RunnerLimits
from test_docker import World

pytestmark = pytest.mark.skipif(
    not os.getenv("AW_OPENCODE_IMAGE_ID"), reason="explicit existing OpenCode image required"
)

CODE = r"""
import contextlib, http.server, io, json, os, runpy, subprocess, sys, threading, time

marks = {}
calls = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(n)
        calls.append(time.perf_counter())
        marks.setdefault("first_request", calls[-1])
        chunk = {
            "id": "startup-fixture",
            "object": "chat.completion.chunk",
            "model": "bound",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Synthetic startup reply."},
                    "finish_reason": None,
                }
            ],
        }
        final = {
            "id": "startup-fixture",
            "object": "chat.completion.chunk",
            "model": "bound",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        wire = (
            "data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)
        self.wfile.flush()


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:" + str(server.server_port)
os.environ["AI_WORKPLACE_MODEL_BASE_URL"] = base + "/v1"
os.environ["AI_WORKPLACE_MAX_MODEL_TOKENS"] = "64"
os.environ["AI_WORKPLACE_WORKSPACE_URL"] = base + "/v1/workspace"
original = subprocess.Popen


def launch(*args, **kwargs):
    marks["spawn"] = time.perf_counter()
    return original(*args, **kwargs)


subprocess.Popen = launch
worker = runpy.run_path("/opt/agent-harness/worker.py", run_name="startup_fixture")
sys.argv = ["worker.py", "Reply once, without tools, with the synthetic startup acknowledgement."]
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    worker["main"]()
ended = time.perf_counter()
outcome = json.loads(buffer.getvalue())
assert outcome["status"] == "completed" and len(calls) == 1
print(
    json.dumps(
        {
            "model_calls": len(calls),
            "spawn_to_first_request_seconds": round(marks["first_request"] - marks["spawn"], 4),
            "spawn_to_cli_complete_seconds": round(ended - marks["spawn"], 4),
            "first_request_to_cli_complete_seconds": round(ended - marks["first_request"], 4),
        }
    )
)
server.shutdown()
"""


def test_cold_opencode_startup_without_platform_or_provider_latency(monkeypatch, record_property):
    async def scenario():
        monkeypatch.setenv("AW_SANDBOX_IMAGE_ID", os.environ["AW_OPENCODE_IMAGE_ID"])
        w = World()
        cap = RunnerLimits(30, 1024, 128, 45, 65536, 32 * 1024 * 1024)
        template = next(iter(w.runner.templates.values()))
        w.runner.templates[template.id] = replace(template, ceiling=cap)
        started = time.perf_counter()
        result = await w.runner.run(w.admission, w.task(CODE, cap=cap))
        assert result.status == "succeeded", result.status
        metric = json.loads(result.stdout_tail)
        assert metric["model_calls"] == 1
        assert (
            0 < metric["spawn_to_first_request_seconds"] <= metric["spawn_to_cli_complete_seconds"]
        )
        metric["runner_total_seconds"] = round(time.perf_counter() - started, 4)
        for name, value in metric.items():
            record_property(name, value)
        print(json.dumps(metric))
        await w.runner.purge()

    asyncio.run(scenario())
