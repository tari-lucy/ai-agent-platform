"""Bounded stdio MCP facade inside the sandbox; all authority remains with host owners."""

import json
import os
import sys
import urllib.request
from typing import Any

SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": [
                "capabilities",
                "list_files",
                "read_file",
                "write_note",
                "raise_ticket",
                "read_ticket",
                "find_skills",
                "read_skill",
                "save_skill",
            ],
        },
        "arguments": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": 4000},
            "maxProperties": 12,
        },
    },
    "required": ["operation", "arguments"],
    "additionalProperties": False,
}


def respond(request: dict[str, Any]) -> dict[str, Any]:
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "platform-workspace", "version": "1"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "workspace",
                    "description": "Use platform owners: capabilities; list_files(area); "
                    "read_file(file_id); write_note(file_id,path,title,body,kind=note|hypothesis, "
                    "expected_version optional); "
                    "raise_ticket(title,body,kind=question|problem|idea,capability_id optional); "
                    "read_ticket(ticket_id); find_skills(query); read_skill(skill_id); "
                    "save_skill(skill_id,title,applicability,method,limitations optional, "
                    "change_reason optional,expected_version optional). "
                    "Arguments are strings. Tickets request access, never grant it. A saved skill "
                    "is experimental: it grants no access, adds no tool and runs nothing. "
                    "No host paths.",
                    "inputSchema": SCHEMA,
                }
            ]
        }
    if method != "tools/call" or not isinstance(params, dict) or params.get("name") != "workspace":
        raise ValueError()
    arguments = params.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {"operation", "arguments"}:
        raise ValueError()
    identifier = str(request["id"])
    if not 1 <= len(identifier) <= 60:
        raise ValueError()
    body = json.dumps(dict(arguments, call_id="mcp-" + identifier)).encode()
    if len(body) > 65536:
        raise ValueError()
    wire = urllib.request.Request(
        os.environ["AI_WORKPLACE_WORKSPACE_URL"],
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
        request = json.loads(line)
        if not isinstance(request, dict):
            break
        if "id" not in request:
            continue
        try:
            result = respond(request)
            answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except ValueError, TypeError, KeyError, OSError:
            answer = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32602, "message": "WORKSPACE_REQUEST_REFUSED"},
            }
        print(json.dumps(answer), flush=True)


if __name__ == "__main__":
    main()
