"""Linux PID 1 script, sent as trusted image entrypoint, not imported on Windows.

This is not a source of authority: the host rechecks every bound, admission and
reference. All task bytes live in tmpfs and disappear with the container.
"""

from .bridge import BRIDGE

STAGE_INPUTS = r"""
manifest = []
for index, item in enumerate(payload["inputs"]):
    name = "input-%04d" % index
    data = base64.b64decode(item["content"], validate=True)
    with open("inputs/" + name, "xb") as stream:
        stream.write(data)
    manifest.append({"name": name, "artifact_id": item["id"], "version": item["version"]})
with open("inputs/manifest.json", "x", encoding="utf-8") as stream:
    json.dump(manifest, stream)
"""

# The persona's persistent home (W2). Agent-installed tools and caches land in the volume;
# OpenCode's own state (config, data, history) stays in tmpfs and goes with the container.
HOME_ENV = r"""
home = payload.get("home")
if home is not None:
    env.update({"AI_WORKPLACE_HOME": "/workspace",
                "PATH": "/workspace/.local/bin:/workspace/.npm-global/bin:" + env["PATH"],
                "PYTHONUSERBASE": "/workspace/.local",
                "NPM_CONFIG_PREFIX": "/workspace/.npm-global",
                "PIP_CACHE_DIR": "/workspace/.cache/pip",
                # Tools OpenCode fetches itself (ripgrep) are kept once, not per turn.
                "XDG_CACHE_HOME": "/workspace/.cache"})
    for directory in ("inbox", "share"):
        os.makedirs("/workspace/" + directory, 0o700, exist_ok=True)
# W3: the turn's egress gateway. Loopback (the model facade) never goes through it.
proxy = payload.get("proxy")
if proxy is not None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[key] = env[key.lower()] = proxy
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    env["AI_WORKPLACE_PROXY"] = proxy
"""

# Written fresh every turn from the platform's copy: changing it inside has no lasting effect.
HOME_GUIDE = r"""
if home is not None:
    def guide_file(name, text):
        with open("/guide/" + name, "x", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod("/guide/" + name, 0o444)

    def head(path, size):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return ""
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return ""
            return stream.read(size).decode("utf-8", "replace")

    def usage(root, budget=100000, seconds=2.0):
        used, seen, pending, until = 0, 0, [root], time.monotonic() + seconds
        while pending:
            if seen >= budget or time.monotonic() > until:
                return used, False
            try:
                entries = list(os.scandir(pending.pop()))
            except OSError:
                continue
            for item in entries:
                seen += 1
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                used += info.st_blocks * 512
                if stat.S_ISDIR(info.st_mode):
                    pending.append(item.path)
        return used, True

    if not os.path.lexists("/workspace/README.md"):
        with open("/workspace/README.md", "x", encoding="utf-8") as stream:
            stream.write(home["readme"])
    used, complete = usage("/workspace")
    mib = used // (1024 * 1024)
    space = "\n\nЗанято в /workspace: %s%d МиБ из рекомендуемых %d МиБ." % (
        "" if complete else "не меньше ", mib, home["quota_mb"])
    if mib > home["quota_mb"]:
        space += " Порог превышен: освободи место (scratch/, кэши, старые сборки)."
    guide_file("AGENTS.md", home["rules"])
    guide_file("connections.md", home["connections"] + space)
    guide_file("workspace-readme.md", "Начало /workspace/README.md (до 8000 байт; "
               "полный файл читай сам):\n\n" + head("/workspace/README.md", 8000))
    os.chmod("/guide", 0o555)
"""

COLLECT_FRAME = r"""
files = []
total = len(output)
if not stat.S_ISDIR(os.lstat("outputs").st_mode):
    state = "failed"
if state == "succeeded":
    for root, directories, names in os.walk("outputs", followlinks=False):
        for directory in directories:
            if os.path.islink(os.path.join(root, directory)):
                state = "failed"
        for name in names:
            path = os.path.join(root, name)
            logical = path[len("outputs/"):]
            if (len(logical) > 256 or len(logical.split("/")) > 8 or
                any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", p) or p in {".", ".."}
                    for p in logical.split("/"))):
                state = "failed"
                break
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    state = "failed"
                    break
                data = stream.read(max(0, cap["output_bytes"]-total)+1)
            total += len(data)
            if total > cap["output_bytes"] or len(files) >= payload["max_files"]:
                state = "limit"
                break
            files.append({"name": logical, "checksum": hashlib.sha256(data).hexdigest(),
                          "content": base64.b64encode(data).decode("ascii")})
        if state != "succeeded":
            break
if state != "succeeded":
    files = []
wire({"kind": "result", "status": state, "exit_code": child.returncode,
                  "stdout": base64.b64encode(output[:cap["output_bytes"]]).decode("ascii"),
                  "files": files})
"""

# No interpreter finalisation while the daemon stdin reader holds a buffered lock.
# The complete frame was flushed; exiting PID 1 tears down this task namespace.
EXIT = r"""
os._exit(0)
"""

ENTRY = (
    r"""
import base64, hashlib, json, os, re, selectors, signal, stat, subprocess, sys, time

os.umask(0o077)
payload = json.loads(sys.stdin.buffer.readline())
cap = payload["limits"]
os.environ.clear()
env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work/home",
       "TMPDIR": "/work/tmp", "XDG_CONFIG_HOME": "/work/config",
       "XDG_CACHE_HOME": "/work/cache", "XDG_DATA_HOME": "/work/data",
       "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
for directory in ("inputs", "outputs", "home", "tmp", "config", "cache", "data"):
    os.mkdir(directory, 0o700)
"""
    + HOME_ENV
    + STAGE_INPUTS
    + HOME_GUIDE
    + BRIDGE
    + r"""
started = time.monotonic()
child = subprocess.Popen(payload["command"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env=env, start_new_session=True)
output = bytearray()
state = "succeeded"
poller = selectors.DefaultSelector()
poller.register(child.stdout, selectors.EVENT_READ)
while poller.get_map():
    if time.monotonic() - started > cap["wall_seconds"]:
        state = "timeout"
        break
    for key, _ in poller.select(0.05):
        part = os.read(key.fd, min(65536, cap["output_bytes"] + 1 - len(output)))
        if not part:
            poller.unregister(key.fileobj)
        else:
            output.extend(part)
    if len(output) > cap["output_bytes"]:
        state = "limit"
        break
if child.poll() is None:
    if state == "succeeded":
        try:
            child.wait(timeout=max(0.01, cap["wall_seconds"] - (time.monotonic()-started)))
        except subprocess.TimeoutExpired:
            state = "timeout"
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
# Kill same-UID detached descendants too. This is a private PID namespace.
# Linux excludes calling process and namespace PID 1 from kill(-1).
try:
    os.kill(-1, signal.SIGKILL)
except ProcessLookupError:
    pass
child.stdout.close()
poller.close()
if child.returncode != 0 and state == "succeeded":
    state = "failed"
"""
    + COLLECT_FRAME
    + EXIT
)
