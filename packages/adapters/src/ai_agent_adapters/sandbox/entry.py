"""Linux PID 1 script, sent as trusted image entrypoint, not imported on Windows.

This is not a source of authority: the host rechecks every bound, admission and
reference. All task bytes live in tmpfs and disappear with the container.
"""

from .bridge import BRIDGE

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
# No interpreter finalisation while the daemon stdin reader holds a buffered lock.
# The complete frame was flushed; exiting PID 1 tears down this task namespace.
os._exit(0)
"""
)
