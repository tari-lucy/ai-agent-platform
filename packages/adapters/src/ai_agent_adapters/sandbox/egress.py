"""Per-turn egress gateway (W3): an internal Docker network and one trusted proxy container.

The agent container joins only the internal network: no DNS and no route out except the
proxy, which resolves names itself and refuses non-public and denied destinations. The host
keeps the request log and removes the proxy and the network with the turn (and on STOP).
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .docker import cli

PROXY = Path(__file__).with_name("egress_proxy.py").read_text(encoding="utf-8")
NETWORK = re.compile(r"aw-net-[0-9a-f]{32}")
MAX_FRAME = 8192
# Mail submission: no spam relay from a workplace, whatever the agent configures inside.
MAIL_PORTS = (25, 465, 587)


@dataclass(frozen=True)
class EgressLimits:
    max_bytes: int = 256 * 1024 * 1024
    max_connections: int = 32
    connect_timeout: float = 15.0
    deny: tuple[str, ...] = ()
    deny_ports: tuple[int, ...] = MAIL_PORTS
    memory_mb: int = 128

    def __post_init__(self) -> None:
        for item in self.deny:
            ipaddress.ip_network(item)  # ValueError for a malformed operator entry
        if (
            not 1 <= self.max_bytes <= 64 * 1024**3
            or not 1 <= self.max_connections <= 256
            or not 1 <= self.connect_timeout <= 60
            or not 32 <= self.memory_mb <= 1024
            or any(not 1 <= port <= 65535 for port in self.deny_ports)
        ):
            raise ValueError("EGRESS_LIMITS_INVALID")

    def wire(self) -> dict[str, Any]:
        return {
            "max_bytes": self.max_bytes,
            "max_connections": self.max_connections,
            "connect_timeout": self.connect_timeout,
            "deny": list(self.deny),
            "deny_ports": list(self.deny_ports),
        }


class Egress:
    """One use: ``open`` returns the proxy URL for the agent, ``close`` removes everything.

    ``record`` receives each request row (method, target, status, bytes); it is local data,
    possibly personal, and never goes to Git or reports.
    """

    def __init__(
        self, image_id: str, limits: EgressLimits, record: Callable[[dict[str, Any]], None]
    ) -> None:
        suffix = uuid4().hex
        self.image_id, self.limits, self.record = image_id, limits, record
        self.network = "aw-net-" + suffix
        self.name = "aw-egress-" + suffix
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.used = 0

    def args(self) -> list[str]:
        memory = f"{self.limits.memory_mb}m"
        return [
            "create",
            "--pull=never",
            "--name",
            self.name,
            "--label",
            "ai-agent-platform.instance=" + self.name,
            "--label",
            "ai-agent-platform.egress=1",
            "--interactive",
            "--network=bridge",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--ipc=none",
            "--log-driver=none",
            "--no-healthcheck",
            "--restart=no",
            "--pids-limit",
            "64",
            "--cpus=1",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--ulimit",
            "nofile=1024:1024",
            "--ulimit",
            "core=0:0",
            "--entrypoint=/usr/local/bin/python",
            self.image_id,
            "-I",
            "-u",
            "-c",
            PROXY,
        ]

    async def open(self) -> str:
        await cli(
            "network", "create", "--internal", "--label", "ai-agent-platform.egress=1",
            self.network,
        )  # fmt: skip
        await cli(*self.args())
        await cli("network", "connect", self.network, self.name)
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
        self.process.stdin.write(json.dumps(self.limits.wire()).encode() + b"\n")
        await self.process.stdin.drain()
        ready = await asyncio.wait_for(self.process.stdout.readline(), 20)
        if ready != b'{"kind": "ready"}\n':
            raise OSError("EGRESS_NOT_READY")
        networks = json.loads(
            await cli("inspect", "--format", "{{json .NetworkSettings.Networks}}", self.name)
        )
        address = ipaddress.ip_address(networks[self.network]["IPAddress"])
        self.reader = asyncio.create_task(self._read())
        return f"http://{address}:3128"

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("kind") == "egress":
                for key in ("up", "down"):
                    if isinstance(row.get(key), int):
                        self.used += row[key]
                row.pop("kind")
                try:
                    self.record(row)
                except OSError:
                    # The log is local diagnostics; a full disk does not stop the turn.
                    logging.getLogger(__name__).warning("Egress log write failed")

    async def close(self) -> bool:
        """True only with removal receipts for both the proxy and the network."""
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        removed = True
        try:
            present = await cli(
                "ps", "--all", "--filter", "label=ai-agent-platform.instance=" + self.name,
                "--format", "{{.ID}}",
            )  # fmt: skip
            if present.strip():
                await cli("rm", "--force", self.name)
        except OSError, TimeoutError:
            removed = False
        try:
            listed = await cli(
                "network", "ls", "--filter", "name=^" + self.network + "$", "--format", "{{.ID}}"
            )
            if listed.strip():
                await cli("network", "rm", self.network)
        except OSError, TimeoutError:
            removed = False
        return removed
