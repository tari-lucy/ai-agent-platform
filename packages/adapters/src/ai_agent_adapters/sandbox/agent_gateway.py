"""Long-lived harness of one persona (D79/D80): the agent gateway and the agent container.

The agent container runs ``opencode serve`` on an internal network with no route out except
the gateways. The agent gateway is a platform container on that network and on the default
bridge: it holds the model key and publishes the OpenCode port on the host's loopback only.
Configuration (key, server password, OpenCode config) goes through stdin, never through
``docker inspect``-visible arguments or environment. The host closes both on STOP.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .docker import cli
from .policy import HOME_VOLUME

GATEWAY = Path(__file__).with_name("agent_gateway_proxy.py").read_text(encoding="utf-8")
MAX_FRAME = 8192
LOG = logging.getLogger(__name__)

# Runs as the agent container's entrypoint: directories, environment, then OpenCode itself.
BOOT = r"""
import json, os, sys
c = json.loads(sys.stdin.readline())
for d in ("/work/home", "/work/tmp", "/work/config", "/work/cache"):
    os.makedirs(d, mode=0o700, exist_ok=True)
os.makedirs("/workspace/.harness/data", mode=0o700, exist_ok=True)
env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work/home", "TMPDIR": "/work/tmp",
       "XDG_CONFIG_HOME": "/work/config", "XDG_CACHE_HOME": "/work/cache",
       "XDG_DATA_HOME": "/workspace/.harness/data", "LANG": "C.UTF-8",
       "OPENCODE_SERVER_PASSWORD": c["password"], "OPENCODE_CONFIG_CONTENT": c["config"],
       "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
       # No project config: the agent cannot rewrite its own rules from /workspace.
       "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
       "OPENCODE_DISABLE_FFF": "1", "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1"}
env.update(c["proxy"])
os.chdir("/workspace")
os.execve("/usr/local/bin/opencode",
          ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"], env)
"""


def _hardening(memory_mb: int, pids: int) -> list[str]:
    memory = f"{memory_mb}m"
    return [
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--ipc=none",
        "--log-driver=none",
        "--no-healthcheck",
        "--restart=no",
        "--pids-limit",
        str(pids),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--ulimit",
        "core=0:0",
    ]


async def _remove(name: str) -> bool:
    try:
        present = await cli(
            "ps", "--all", "--filter", "label=ai-agent-platform.instance=" + name,
            "--format", "{{.ID}}",
        )  # fmt: skip
        if present.strip():
            await cli("rm", "--force", name, timeout=30)
        return True
    except OSError, TimeoutError:
        return False


async def _addresses(name: str) -> dict[str, str]:
    networks = json.loads(
        await cli("inspect", "--format", "{{json .NetworkSettings.Networks}}", name)
    )
    return {
        network: str(ipaddress.ip_address(item["IPAddress"]))
        for network, item in networks.items()
        if item.get("IPAddress")
    }


class AgentGateway:
    """One use: ``open`` returns the model URL for the agent, ``close`` removes it."""

    def __init__(self, image_id: str, port: int, record: Callable[[dict[str, Any]], None]) -> None:
        if not 1024 <= port <= 65535:
            raise ValueError("HARNESS_PORT_INVALID")
        self.image_id, self.port, self.record = image_id, port, record
        self.name = "aw-hgw-" + uuid4().hex
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None

    def args(self) -> list[str]:
        return [
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--label",
            "ai-agent-platform.instance=" + self.name,
            "--label",
            "ai-agent-platform.harness=1",
            "--interactive",
            "--network=bridge",
            "--publish",
            f"127.0.0.1:{self.port}:4096",
            *_hardening(256, 256),
            "--cpus=1",
            "--ulimit",
            "nofile=4096:4096",
            "--entrypoint=/usr/local/bin/python",
            self.image_id,
            "-I",
            "-u",
            "-c",
            GATEWAY,
        ]

    async def open(
        self, network: str, *, upstream: str, key: str, model: str, remaining: float | None
    ) -> str:
        await cli(*self.args())
        await cli("network", "connect", network, self.name)
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "--attach",
            "--interactive",
            self.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_FRAME,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        addresses: dict[str, str] = {}
        for _ in range(100):  # the addresses exist once the container runs
            addresses = await _addresses(self.name)
            if network in addresses and "bridge" in addresses:
                break
            await asyncio.sleep(0.1)
        else:
            raise OSError("HARNESS_GATEWAY_ADDRESS")
        self._send(
            {
                "upstream": upstream,
                "key": key,
                "model": model,
                "remaining": remaining,
                "model_bind": addresses[network],
                "ui_bind": addresses["bridge"],
            }
        )
        await self.process.stdin.drain()
        ready = await asyncio.wait_for(self.process.stdout.readline(), 30)
        if json.loads(ready or b"{}") != {"kind": "ready"}:
            raise OSError("HARNESS_GATEWAY_NOT_READY")
        self.reader = asyncio.create_task(self._read())
        return f"http://{addresses[network]}:8080/v1"

    def _send(self, row: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            return
        with contextlib.suppress(OSError, RuntimeError):
            self.process.stdin.write(json.dumps(row).encode() + b"\n")

    def budget(self, remaining: float | None) -> None:
        self._send({"kind": "budget", "remaining": remaining})

    def agent(self, address: str) -> None:
        self._send({"kind": "agent", "address": address})

    def stop(self) -> None:
        self._send({"kind": "stop"})

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                try:
                    self.record(row)
                except OSError:
                    LOG.warning("Harness usage write failed")

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self) -> bool:
        self.stop()
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        return await _remove(self.name)


class HarnessContainer:
    """The persona's ``opencode serve``: its home volume, an internal network, no keys."""

    def __init__(
        self,
        image_id: str,
        home: str,
        *,
        memory_mb: int,
        pids: int,
        shared: dict[str, str] | None = None,
    ) -> None:
        """``shared``: operator folders on the host disk mounted over ``/workspace/<name>``
        (``inbox``/``share``), so files move by drag and drop; the rest stays in the volume."""
        if HOME_VOLUME.fullmatch(home) is None:
            raise ValueError("RUNNER_HOME_INVALID")
        if any(
            name not in ("inbox", "share") or "," in path for name, path in (shared or {}).items()
        ):
            raise ValueError("HARNESS_SHARED_INVALID")
        self.shared = dict(shared or {})
        self.image_id, self.home = image_id, home
        self.memory_mb, self.pids = memory_mb, pids
        self.name = "aw-harness-" + uuid4().hex
        self.network = "aw-net-" + uuid4().hex
        self.process: asyncio.subprocess.Process | None = None
        self.own_network = False

    def args(self, network: str) -> list[str]:
        if re.fullmatch(r"aw-net-[0-9a-f]{32}", network) is None:
            raise ValueError("RUNNER_NETWORK_INVALID")
        return [
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--label",
            "ai-agent-platform.instance=" + self.name,
            "--label",
            "ai-agent-platform.harness=1",
            "--interactive",
            "--network=" + network,
            *_hardening(self.memory_mb, self.pids),
            "--cpus=2",
            "--ulimit",
            "nofile=8192:8192",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=1073741824,nr_inodes=65536,mode=0700,uid=65532,gid=65532",
            "--mount",
            f"type=volume,src={self.home},dst=/workspace",
            *(
                item
                for name, path in sorted(self.shared.items())
                for item in ("--mount", f"type=bind,src={path},dst=/workspace/{name}")
            ),
            "--workdir=/workspace",
            "--entrypoint=/usr/local/bin/python",
            self.image_id,
            "-I",
            "-u",
            "-c",
            BOOT,
        ]

    async def network_open(self) -> str:
        await cli(
            "network", "create", "--internal", "--label", "ai-agent-platform.harness=1",
            self.network,
        )  # fmt: skip
        self.own_network = True
        return self.network

    async def open(
        self, network: str, *, password: str, config: dict[str, Any], proxy: dict[str, str]
    ) -> str:
        await cli("volume", "create", "--label", "ai-agent-platform.home=1", self.home, cap=1024)
        await cli(*self.args(network))
        self.process = await asyncio.create_subprocess_exec(
            "docker",
            "start",
            "--attach",
            "--interactive",
            self.name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {"password": password, "config": json.dumps(config), "proxy": proxy}
            ).encode()
            + b"\n"
        )
        await self.process.stdin.drain()
        for _ in range(100):
            addresses = await _addresses(self.name)
            if network in addresses:
                return addresses[network]
            await asyncio.sleep(0.1)
        raise OSError("HARNESS_AGENT_ADDRESS")

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self) -> bool:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        return await _remove(self.name)

    async def close_network(self) -> bool:
        """After the gateway left it: an attached container keeps a network from removal."""
        removed = True
        if self.own_network:
            try:
                listed = await cli(
                    "network", "ls", "--filter", "name=^" + self.network + "$",
                    "--format", "{{.ID}}",
                )  # fmt: skip
                if listed.strip():
                    await cli("network", "rm", self.network)
            except OSError, TimeoutError:
                removed = False
        return removed


async def reclaim_harness() -> bool:
    """After a crash: remove every harness container left by a previous host process."""
    try:
        listed = await cli(
            "ps", "--all", "--filter", "label=ai-agent-platform.harness=1", "--format", "{{.Names}}"
        )
    except OSError, TimeoutError:
        return False
    ok = True
    for name in listed.decode().split():
        ok = await _remove(name) and ok
    try:
        networks = await cli(
            "network", "ls", "--filter", "label=ai-agent-platform.harness=1", "--format",
            "{{.Name}}",
        )  # fmt: skip
        for network in networks.decode().split():
            await cli("network", "rm", network)
    except OSError, TimeoutError:
        ok = False
    return ok
