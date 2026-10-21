"""Container loopback Chat Completions facade. Stdio is the only upstream transport."""

BRIDGE = r"""
import http.server, queue, threading
wire_lock = threading.Lock()
rpc_slot = threading.Lock()
replies = queue.Queue(maxsize=1)

def wire(value):
    with wire_lock:
        print(json.dumps(value, separators=(",", ":")), flush=True)

def responses():
    for line in sys.stdin.buffer:
        try:
            replies.put_nowait(json.loads(line))
        except (ValueError, queue.Full):
            break

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_json(404, {"error": "ROUTE_NOT_AVAILABLE"})
            return
        if not rpc_slot.acquire(blocking=False):
            self.send_json(429, {"error": "MODEL_BUSY"})
            return
        try:
            self.connection.settimeout(cap["wall_seconds"])
            size = int(self.headers.get("Content-Length", "-1"))
            if not 0 < size <= payload["max_prompt_chars"] * 6 + 8192:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            allowed = {"messages", "model", "max_tokens", "max_completion_tokens",
                       "stream", "tools", "stream_options", "tool_choice"}
            if any(key not in allowed for key in body):
                self.send_json(400, {"error": "MODEL_OPTION_UNSUPPORTED"})
                return
            options = body.get("stream_options", {})
            if (body.get("tool_choice", "auto") != "auto"
                    or not isinstance(options, dict) or set(options) - {"include_usage"}
                    or type(options.get("include_usage", False)) is not bool):
                self.send_json(400, {"error": "MODEL_OPTION_UNSUPPORTED"})
                return
            wire({"kind": "model", "messages": body["messages"],
                  "tools": body.get("tools", []),
                  "max_tokens": body.get("max_completion_tokens",
                      body.get("max_tokens", payload["max_model_tokens"]))})
            answer = replies.get(timeout=cap["wall_seconds"])
            if answer.get("status") != "completed":
                self.send_json(429, {"error": "MODEL_NOT_ADMITTED"})
                return
            text = answer["text"]
            message = {"role": "assistant", "content": text}
            tool_calls = answer.get("tool_calls", [])
            reason = "tool_calls" if tool_calls else "stop"
            if tool_calls:
                message["tool_calls"] = tool_calls
            response = {"id": "task-model", "object": "chat.completion", "model": "bound-role",
                        "choices": [{"index": 0, "message": message, "finish_reason": reason}]}
            if body.get("stream") is True:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if tool_calls:
                    message = dict(message, tool_calls=[dict(call, index=index)
                                   for index, call in enumerate(tool_calls)])
                chunk = {"id": "task-model", "object": "chat.completion.chunk",
                         "model": "bound-role",
                         "choices": [{"index": 0, "delta": message, "finish_reason": None}]}
                final = {"id": "task-model", "object": "chat.completion.chunk",
                         "model": "bound-role",
                         "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) +
                                  "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            else:
                self.send_json(200, response)
        except (ValueError, KeyError, TypeError, OSError, queue.Empty):
            try:
                self.send_json(400, {"error": "MODEL_REQUEST_INVALID"})
            except OSError:
                pass
        finally:
            rpc_slot.release()

if payload["gateway"]:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=responses, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env["AI_WORKPLACE_MODEL_BASE_URL"] = "http://127.0.0.1:%d/v1" % server.server_port
    env["AI_WORKPLACE_MAX_MODEL_TOKENS"] = str(payload["max_model_tokens"])
"""
