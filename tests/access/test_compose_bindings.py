"""Static deployment bindings remain purpose-separated; dynamic render is a separate probe."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_compose_passes_only_worker_token_to_worker_and_host_port_to_core():
    text = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    core = text.split("  core:")[1].split("  worker:")[0]
    worker = text.split("  worker:")[1].split("  redis:")[0]
    assert "AI_PLATFORM_OPERATOR_PASSWORD_HASH:" in core
    assert "AI_PLATFORM_WORKER_SERVICE_TOKEN:" in core
    assert "AI_PLATFORM_CORE_HOST_PORT:" in core
    assert "AI_PLATFORM_WORKER_SERVICE_TOKEN:" in worker
    assert "AI_PLATFORM_OPERATOR_PASSWORD_HASH:" not in worker
    assert '"127.0.0.1:${AI_PLATFORM_CORE_HOST_PORT:-8765}:' in core


def test_deployment_limits_websocket_frames_queue_and_disables_access_logs():
    source = ast.parse((ROOT / "deploy/core.py").read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
    )
    args = {
        arg.arg: ast.literal_eval(arg.value)
        for arg in call.keywords
        if isinstance(arg.value, ast.Constant)
    }
    assert args["ws"] == "websockets"
    assert args["ws_per_message_deflate"] is False
    assert args["ws_max_size"] == 4096
    assert args["ws_max_queue"] == 1
    assert args["access_log"] is False
