"""The actual container facade must not label terminal model failures as retryable 429."""

import io
import json
from types import SimpleNamespace

import pytest
from ai_agent_adapters.sandbox.bridge import BRIDGE


@pytest.mark.parametrize(
    "status,http_code,error",
    [
        ("incomplete", 400, "MODEL_RESULT_UNAVAILABLE"),
        ("failed", 400, "MODEL_RESULT_UNAVAILABLE"),
        ("unknown", 400, "MODEL_RESULT_UNAVAILABLE"),
        ("cancelled", 403, "MODEL_NOT_ADMITTED"),
        ("not_admitted", 403, "MODEL_NOT_ADMITTED"),
    ],
)
def test_container_handler_does_not_retry_terminal_host_result(status, http_code, error):
    namespace = {
        "json": json,
        "payload": {"gateway": False, "max_prompt_chars": 1000, "max_model_tokens": 128},
        "cap": {"wall_seconds": 2},
    }
    exec(BRIDGE, namespace)
    frames = []
    namespace["wire"] = frames.append
    namespace["replies"].put_nowait({"status": status, "text": ""})
    handler = object.__new__(namespace["Handler"])
    body = json.dumps({"messages": [{"role": "user", "content": "synthetic"}]}).encode()
    handler.path = "/v1/chat/completions"
    handler.headers = {"Content-Length": str(len(body))}
    handler.connection = SimpleNamespace(settimeout=lambda _: None)
    handler.rfile = io.BytesIO(body)
    responses = []
    handler.send_json = lambda status, body: responses.append((status, body))
    handler.do_POST()
    assert len(frames) == 1
    assert responses == [(http_code, {"error": error})]
