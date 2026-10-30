import json
import os
import shlex
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer synthetic-p1-model-key":
            self.send_error(403)
            return
        messages = body["messages"]
        last = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=0)
        text = str(messages[last].get("content", ""))
        done = any(m["role"] == "tool" for m in messages[last + 1 :])
        tool = None
        args = {}
        reply = "Synthetic P1 reply."
        if "⏰" in text:
            reply = "P1 alarm delivered."
        elif "P1-WAKE" in text and not done:
            tool = next(
                t["function"]["name"]
                for t in body["tools"]
                if t["function"]["name"].endswith("wake_me")
            )
            args = {"minutes": 1, "text": "P1-ALARM: synthetic reminder"}
        elif "P1-STOP" in text and not done:
            tool = "bash"
            code = (
                "import time,pathlib;time.sleep(60);"
                "pathlib.Path('/workspace/projects/p1-late.txt').write_text('late')"
            )
            args = {
                "command": "python -c " + shlex.quote(code),
                "description": "P1 synthetic STOP probe",
                "timeout": 90000,
            }
        elif "P1-FILE" in text and not done:
            tool = "bash"
            code = (
                "from pathlib import Path;"
                "print(Path('/workspace/inbox/p1-marker.txt').read_text());"
                "Path('/workspace/projects/p1-result.txt').write_text('P1-RESULT')"
            )
            args = {
                "command": "python -c " + shlex.quote(code),
                "description": "P1 synthetic file probe",
            }
        elif "P1-FILE" in text:
            reply = "P1-MARKER confirmed; P1-RESULT saved."
        delta = {"role": "assistant", "content": reply}
        reason = "stop"
        if tool:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_" + str(time.time_ns()),
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(args)},
                    }
                ],
            }
            reason = "tool_calls"
        base = {"id": "chatcmpl-p1", "created": int(time.time()), "model": body["model"]}
        usage = {"prompt_tokens": 100, "completion_tokens": 10}
        if body.get("stream"):
            events = [
                {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                },
                {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
            ]
            data = (
                "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
            ).encode()
            kind = "text/event-stream"
        else:
            data = json.dumps(
                {
                    **base,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": delta, "finish_reason": reason}],
                    "usage": usage,
                }
            ).encode()
            kind = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    if os.environ.get("P1_FIXTURE_ONLY") != "1":
        raise SystemExit("Docker fixture flag required")
    ThreadingHTTPServer(("0.0.0.0", 41412), Handler).serve_forever()
