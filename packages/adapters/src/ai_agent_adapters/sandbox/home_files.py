"""Operator access to a persona's home volume (W4) through short-lived networkless containers.

The host never mounts or resolves the volume itself (on Windows it lives inside Docker's VM).
Listing and reading mount it read-only; the only write is a new file in ``inbox/``. Paths are
logical, relative to /workspace; links and special files are shown, never followed or read.
"""

import json
import re
from typing import Any

from .docker import cli
from .policy import HOME_VOLUME

MAX_ENTRIES = 1000
MAX_READ = 1024 * 1024
MAX_UPLOAD = 100 * 1024 * 1024
SEGMENT = re.compile(r"[^/\x00-\x1f\x7f]{1,255}")

# Runs as PID 1 of the helper container; payload is the first stdin line, bytes may follow.
SCRIPT = r"""
import json, os, stat, sys
request = json.loads(sys.stdin.buffer.readline())
root = "/workspace"

def walk(relative):
    # Each segment opened without following links: a link inside the volume is never a door out.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    for part in [p for p in relative.split("/") if p]:
        try:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        finally:
            os.close(fd)
        fd = nxt
    return fd

def kind(mode):
    return ("directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode)
            else "link" if stat.S_ISLNK(mode) else "other")

def answer(value):
    sys.stdout.write(json.dumps(value))
    sys.stdout.flush()
    os._exit(0)

try:
    op = request["op"]
    if op == "list":
        fd = walk(request["path"])
        rows = []
        with os.scandir(fd) as entries:
            for item in entries:
                if len(rows) >= request["max"]:
                    break
                info = item.stat(follow_symlinks=False)
                rows.append({"name": item.name, "type": kind(info.st_mode),
                             "size": info.st_size, "mtime": int(info.st_mtime)})
        answer({"status": "ok", "entries": rows, "complete": len(rows) < request["max"]})
    head, _, name = request["path"].rpartition("/")
    fd = walk(head)
    if op == "read":
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode):
            answer({"status": "not_file"})
        data = os.read(file, request["max"])
        import base64
        answer({"status": "ok", "size": info.st_size,
                "content": base64.b64encode(data).decode("ascii")})
    if op == "put":
        # Only inbox/ of the home; never overwrite: the operator gets a new name instead.
        file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                       dir_fd=fd)
        left = request["size"]
        while left:
            chunk = sys.stdin.buffer.read(min(left, 1 << 20))
            if not chunk:
                os.close(file)
                os.unlink(name, dir_fd=fd)
                answer({"status": "short"})
            os.write(file, chunk)
            left -= len(chunk)
        os.close(file)
        answer({"status": "ok"})
    answer({"status": "invalid"})
except FileNotFoundError:
    answer({"status": "missing"})
except FileExistsError:
    answer({"status": "exists"})
except (NotADirectoryError, OSError, KeyError, ValueError):
    answer({"status": "denied"})
"""


def logical(path: str) -> str:
    """'' for the root; ValueError for '..', '.', empty segments or control characters."""
    parts = [part for part in path.strip("/").split("/") if part] if path.strip("/") else []
    if len(path) > 1024 or any(
        part in (".", "..") or SEGMENT.fullmatch(part) is None for part in parts
    ):
        raise ValueError("HOME_PATH_INVALID")
    return "/".join(parts)


class HomeFiles:
    def __init__(self, image_id: str) -> None:
        self.image_id = image_id

    async def _exists(self, volume: str) -> bool:
        listed = await cli(
            "volume", "ls", "--filter", "name=^" + volume + "$", "--format", "{{.Name}}"
        )
        return listed.strip().decode() == volume

    async def _run(
        self, volume: str, request: dict[str, object], data: bytes = b""
    ) -> dict[str, Any]:
        if HOME_VOLUME.fullmatch(volume) is None:
            raise ValueError("HOME_VOLUME_INVALID")
        writable = request["op"] == "put"
        mount = f"type=volume,src={volume},dst=/workspace" + ("" if writable else ",readonly")
        raw = await cli(
            "run", "--rm", "--interactive", "--pull=never", "--network=none", "--read-only",
            "--user=65532:65532", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--ipc=none", "--log-driver=none", "--pids-limit", "16", "--memory", "128m",
            "--memory-swap", "128m", "--label", "ai-agent-platform.home-files=1",
            "--mount", mount, "--entrypoint=/usr/local/bin/python", self.image_id,
            "-I", "-c", SCRIPT,
            data=json.dumps(request).encode() + b"\n" + data,
            cap=MAX_READ * 2 + 65536,
            # Upload carries binary stdin, not a model prompt. Allow bounded time
            # for a 100 MiB pipe over Desktop/WSL; read/list keep their short limit.
            timeout=9 + (len(data) + (2 << 20) - 1) // (2 << 20),
        )  # fmt: skip
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("HOME_ANSWER_INVALID")
        return result

    async def list(self, volume: str, path: str) -> dict[str, Any]:
        if not await self._exists(volume):
            return {"status": "ok", "entries": [], "complete": True}
        return await self._run(volume, {"op": "list", "path": logical(path), "max": MAX_ENTRIES})

    async def read(self, volume: str, path: str) -> dict[str, Any]:
        clean = logical(path)
        if not clean or not await self._exists(volume):
            return {"status": "missing"}
        return await self._run(volume, {"op": "read", "path": clean, "max": MAX_READ})

    async def put(self, volume: str, name: str, data: bytes) -> dict[str, Any]:
        clean = logical(name)
        if not clean or "/" in clean or not 0 < len(data) <= MAX_UPLOAD:
            raise ValueError("HOME_UPLOAD_INVALID")
        if not await self._exists(volume):
            return {"status": "missing"}
        return await self._run(
            volume, {"op": "put", "path": "inbox/" + clean, "size": len(data)}, data
        )
