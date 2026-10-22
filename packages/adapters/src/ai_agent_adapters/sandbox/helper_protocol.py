"""Closed helper RPC, independent of the workspace/skills operation window."""

from typing import Any

from ai_agent_application.agent_workplace.ports import WorkspaceRequest


def helper_request(row: Any) -> WorkspaceRequest:
    if (
        not isinstance(row, dict)
        or set(row) != {"kind", "call_id", "operation", "arguments"}
        or row["kind"] != "helper"
        or not isinstance(row["call_id"], str)
        or not 1 <= len(row["call_id"]) <= 64
        or row["operation"] not in ("save_script", "run_script")
        or not isinstance(row["arguments"], dict)
        or len(row["arguments"]) > 8
        or any(
            not isinstance(k, str)
            or not 1 <= len(k) <= 64
            or not isinstance(v, str)
            or len(v) > 4000
            for k, v in row["arguments"].items()
        )
    ):
        raise ValueError("RUNNER_HELPER_REQUEST_INVALID")
    return WorkspaceRequest(row["call_id"], row["operation"], row["arguments"])
