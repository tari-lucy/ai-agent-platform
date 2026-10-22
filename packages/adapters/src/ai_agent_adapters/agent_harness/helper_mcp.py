"""Local MCP for managed Python scripts; host chooses images, rights, inputs and limits."""

import json
import os
import sys
import urllib.request
from typing import Any


def respond(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "platform-helpers", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "scripts",
                    "description": "Save and run managed Python helpers in a separate sandbox. "
                    "save_script(file_id,body,expected_version optional) stores a script. "
                    "run_script(file_id,version,inputs optional JSON array of {id,version}, "
                    "argv optional JSON array of strings) runs without network or host paths. "
                    "version is a string. Input 0 is the script; data starts at inputs/input-0001; "
                    "inputs/manifest.json maps exact references. Write results under outputs/. "
                    "Execution needs a separate configured capability and permission. "
                    "Outputs and diagnostics return as file references, not proof of correctness.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["save_script", "run_script"]},
                            "arguments": {
                                "type": "object",
                                "maxProperties": 8,
                                "additionalProperties": {"type": "string", "maxLength": 4000},
                            },
                        },
                        "required": ["operation", "arguments"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
    params = request.get("params")
    if method != "tools/call" or not isinstance(params, dict) or params.get("name") != "scripts":
        raise ValueError()
    args = params.get("arguments")
    identifier = str(request["id"])
    if (
        not isinstance(args, dict)
        or set(args) != {"operation", "arguments"}
        or not 1 <= len(identifier) <= 60
    ):
        raise ValueError()
    body = json.dumps(dict(args, call_id="mcp-" + identifier)).encode()
    if len(body) > 65536:
        raise ValueError()
    wire = urllib.request.Request(
        os.environ["AI_WORKPLACE_HELPER_URL"],
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(wire, timeout=60) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError()
    answer = json.loads(raw)
    ok = answer.get("status") == "completed"
    text = answer.get("text", "") if ok else "REFUSED:" + str(answer.get("code", "UNAVAILABLE"))
    return {"content": [{"type": "text", "text": text}], "isError": not ok}


def main() -> None:
    while line := sys.stdin.buffer.readline(65537):
        if len(line) > 65536:
            break
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                break
            if "id" not in request:
                continue
            result = respond(request)
            answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except ValueError, TypeError, KeyError, OSError:
            answer = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32602, "message": "HELPER_REQUEST_REFUSED"},
            }
        print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    main()
