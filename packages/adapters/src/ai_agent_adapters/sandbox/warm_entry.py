"""Trusted prewarmed OpenCode entry. No input or model call during preparation.

One-shot (default): one activation, then every process is killed and the container exits.
Resident (W5): the same ``opencode serve`` answers turn after turn; background processes of
the agent survive between turns, while each turn gets a fresh OpenCode session with the
platform's released context (no history accumulates inside). Between turns the facade
refuses model and owner calls. The host closes the container on STOP, idle or any doubt.
"""

import textwrap

from .bridge import BRIDGE
from .entry import COLLECT_FRAME, EXIT, HOME_ENV, HOME_GUIDE, STAGE_INPUTS

PREPARE = (
    r"""
import base64, hashlib, json, os, re, shutil, signal, stat, subprocess, sys, time
import runpy, types, urllib.request
os.umask(0o077)
payload = json.loads(sys.stdin.buffer.readline())
cap = payload["limits"]
resident = payload.get("resident") is True
os.environ.clear()
env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work/home",
       "TMPDIR": "/work/tmp", "XDG_CONFIG_HOME": "/work/config",
       "XDG_CACHE_HOME": "/work/cache", "XDG_DATA_HOME": "/work/data",
       "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
for directory in ("inputs", "outputs", "home", "tmp", "config", "cache", "data"):
    os.mkdir(directory, 0o700)
"""
    + HOME_ENV
    + BRIDGE
    + r"""
worker = runpy.run_path("/opt/agent-harness/worker.py", run_name="warm_configuration")
backend_env = dict(env)
backend_env.update({key: "1" for key in (
    "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH",
    "OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_AUTOCOMPACT",
    "OPENCODE_DISABLE_PRUNE", "OPENCODE_DISABLE_FFF",
    "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER")})
backend_env["XDG_CONFIG_HOME"] = "/opt/agent-harness/config"
backend_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(worker["configuration"](
    env["AI_WORKPLACE_MODEL_BASE_URL"], payload["max_model_tokens"],
    workspace=payload["workspace"], helpers=payload["helper"],
    **({"home": True} if home is not None else {}),
    **({"internet": True} if proxy is not None else {})))
backend = subprocess.Popen(["/usr/local/bin/opencode", "serve", "--hostname", "127.0.0.1",
                            "--port", "4100"], env=backend_env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

def http(path, data=None, timeout=None, limit=None, method=None):
    request = urllib.request.Request("http://127.0.0.1:4100" + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}, method=method)
    wait = cap["wall_seconds"] if timeout is None else timeout
    limit = cap["output_bytes"] * 4 if limit is None else limit
    with urllib.request.urlopen(request, timeout=wait) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("WARM_RESULT_LIMIT")
        return json.loads(raw)

started = time.monotonic()
while True:
    try:
        http("/global/health", timeout=0.5)
        break
    except OSError:
        if backend.poll() is not None or time.monotonic() - started > 20:
            raise SystemExit(2)
        time.sleep(0.05)
# No prompt: initialise the instance, registered tools and agents only.
http("/mcp")
http("/agent")
prepared = payload
if resident:
    turn_open.clear()
wire({"kind": "warm_ready"})
"""
)

ACTIVATE = r"""
activation = replies.get(timeout=wait)
if set(activation) != {"inputs", "command", "limits", "max_files", "gateway",
                       "max_prompt_chars", "max_model_tokens", "workspace", "helper", "home"}:
    raise SystemExit(2)
if any(activation[key] != prepared[key] for key in activation
       if key not in {"inputs", "command", "home"}):
    raise SystemExit(2)
# The volume was mounted at preparation; the guide is this turn's fresh copy.
if (activation["home"] is None) != (home is None):
    raise SystemExit(2)
payload = activation
home = payload["home"]
"""

# A resident turn starts from empty task directories and a rewritable guide.
RESET = r"""
for directory in ("inputs", "outputs"):
    shutil.rmtree(directory)
    os.mkdir(directory, 0o700)
if home is not None:
    os.chmod("/guide", 0o700)
    for name in os.listdir("/guide"):
        os.unlink("/guide/" + name)
"""

TURN = r"""
state = "failed"
child = types.SimpleNamespace(returncode=1)
output = bytearray()
session = None
try:
    instruction = "".join(payload["command"][3:])
    if not instruction.strip() or len(instruction) > worker["MAX_INSTRUCTION"]:
        raise ValueError()
    session = http("/session", {"title": "Platform work session"})
    reply = http("/session/" + session["id"] + "/message", {
        "model": {"providerID": "platform", "modelID": "bound"}, "agent": "platform",
        "parts": [{"type": "text", "text": instruction}]})
    info, parts = reply["info"], reply["parts"]
    if (info.get("role") != "assistant" or info.get("error") is not None
            or info.get("finish") != "stop" or not isinstance(parts, list)):
        raise ValueError()
    text = "".join(part["text"] for part in parts if part.get("type") == "text")
    if not text.strip() or len(text) > worker["MAX_TEXT"]:
        raise ValueError()
    # Each loop step is its own assistant message: the journal reads all of them.
    # Tool outputs are whole here (a read file), so the listing has its own bound.
    if "Events" in worker:
        events = worker["Events"]()
        for row in http("/session/" + session["id"] + "/message",
                        limit=worker["MAX_LINE_BYTES"]):
            if row["info"].get("role") == "assistant":
                for part in row["parts"]:
                    step = worker["tool_step"](part)
                    if step is not None:
                        events.add(step)
        steps, omitted = worker["journal"](events.steps, events.omitted)
        trace = {"tool_errors": sorted(events.denied), "steps": steps,
                 "steps_omitted": omitted}
    else:
        # An image built before the journal: the previous final-message errors only.
        trace = {"tool_errors": sorted({
            part.get("tool") if part.get("tool") in {"read", "write", "edit", "bash"}
            else "tool" for part in parts if part.get("type") == "tool"
            and part.get("state", {}).get("status") == "error"})}
    output = bytearray(json.dumps({"status": "completed", "text": text, **trace},
                                  ensure_ascii=False).encode())
    if len(output) > cap["output_bytes"]:
        raise ValueError()
    state, child.returncode = "succeeded", 0
except (OSError, ValueError, KeyError, TypeError, RecursionError):
    output = bytearray(b'{"status":"failed","code":"OPENCODE_RESULT_INVALID"}')
"""

# The turn's conversation must not outlive it: the next turn has the platform's context only.
FORGET = r"""
if session is not None:
    try:
        http("/session/" + session["id"], method="DELETE", timeout=10)
    except (OSError, ValueError):
        state = "failed"
# No model or owner call is in flight past this point; the facade stays shut until next turn.
with rpc_slot:
    turn_open.clear()
"""

KILL = r"""
# One use only: kill backend/MCP and all same-UID descendants before collecting outputs.
try:
    os.kill(-1, signal.SIGKILL)
except ProcessLookupError:
    pass
"""

ONE_SHOT = (
    "\nwait = payload['warm_idle_seconds']\n"
    + ACTIVATE
    + STAGE_INPUTS
    + HOME_GUIDE
    + TURN
    + KILL
    + COLLECT_FRAME
    + EXIT
)

RESIDENT = r"""
wait = payload["warm_idle_seconds"]
while True:
""" + textwrap.indent(
    ACTIVATE
    + RESET
    + STAGE_INPUTS
    + HOME_GUIDE
    + "turn_open.set()\n"
    + TURN
    + FORGET
    + COLLECT_FRAME
    + "wait = payload_idle\n",
    "    ",
)

WARM_ENTRY = (
    PREPARE
    + r"""
payload_idle = prepared.get("resident_idle_seconds", 3600)
if not resident:
"""
    + textwrap.indent(ONE_SHOT, "    ")
    + "\n"
    + RESIDENT
)
