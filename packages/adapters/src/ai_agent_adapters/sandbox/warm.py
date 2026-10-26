"""One prepared sandbox; never a pool of conversations or authority.

One-shot by default. ``resident`` (W5) keeps it for further turns of the same persona home:
each turn still claims it, gets a fresh OpenCode session and releases it on a clean result;
any doubt (failure, cancel, timeout) closes it. The first wait for a turn uses
``idle_seconds``; later idle time is ``resident_idle_seconds``, both enforced by the host.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from uuid import uuid4

from ai_agent_application.agent_workplace.ports import RunnerTask
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode

from .docker import DockerTaskRunner, cli
from .egress import Egress
from .policy import Home, RunnerTemplate, create_args
from .warm_entry import WARM_ENTRY


def settings(
    template: RunnerTemplate, *, workspace: bool, helper: bool, home: Home | None = None
) -> dict[str, object]:
    return {
        "inputs": [],
        "command": [],
        "limits": asdict(template.ceiling),
        "max_files": template.max_files,
        "gateway": True,
        "max_prompt_chars": template.max_prompt_chars,
        "max_model_tokens": template.max_model_tokens,
        "workspace": workspace,
        "helper": helper,
        "home": home.wire() if home is not None else None,
    }


class WarmSandbox:
    def __init__(
        self,
        template: RunnerTemplate,
        *,
        workspace: bool,
        helper: bool,
        idle_seconds: float,
        valid: Callable[[], None],
        registered: Callable[[str], Awaitable[None]],
        released: Callable[[str, bool], Awaitable[None]],
        home: Home | None = None,
        resident: bool = False,
        resident_idle_seconds: float = 1800,
        egress: Callable[[], Egress] | None = None,
    ) -> None:
        if type(idle_seconds) not in (float, int) or not 10 <= idle_seconds <= 300:
            raise ValueError("WARM_IDLE_INVALID")
        if type(resident_idle_seconds) not in (float, int) or not (
            60 <= resident_idle_seconds <= 4 * 3600
        ):
            raise ValueError("WARM_IDLE_INVALID")
        self.resident, self.resident_idle_seconds = resident, resident_idle_seconds
        self.turns = 0
        self.released_at = 0.0
        # W5b: the gateway lives and dies with this sandbox (network fixed at creation).
        self.egress = egress
        self.gateway: Egress | None = None
        self.internet = egress is not None
        self.template, self.workspace, self.helper = template, workspace, helper
        # Mounted at creation: a prepared sandbox serves only this persona's home.
        self.home = home
        self.home_volume = home.volume if home is not None else None
        self.idle_seconds, self.valid = idle_seconds, valid
        self.registered, self.released = registered, released
        self.name = "aw-task-" + uuid4().hex
        self.process: asyncio.subprocess.Process | None = None
        self.ready = False
        self.claimed = False
        self.closed = False
        self.claimed_at = 0.0
        self.created_at = time.monotonic()
        self._registered = False
        self._create_unknown = False
        self._lock = asyncio.Lock()

    async def prepare(self) -> None:
        task = RunnerTask(
            self.template.id,
            self.template.executable_prefix,
            (),
            self.template.ceiling,
            network="gateway",
        )
        self.valid()
        if self.home_volume is None:
            await DockerTaskRunner._image(self.template)
        else:
            await DockerTaskRunner._image(self.template, home=True)
            await DockerTaskRunner.ensure_home(self.home_volume)
        self.valid()
        await self.registered(self.name)
        self._registered = True
        try:
            async with asyncio.timeout(35):
                proxy = None
                if self.egress is not None:
                    gateway = self.gateway = self.egress()
                    proxy = await gateway.open()
                args = create_args(
                    self.template,
                    task,
                    self.name,
                    WARM_ENTRY,
                    self.home_volume,
                    self.gateway.network if self.gateway is not None else None,
                )
                args[1:1] = ["--label", "ai-agent-platform.instance=" + self.name]
                # Cancellation of the CLI client is not cancellation at Docker daemon.
                self._create_unknown = True
                creation = asyncio.create_task(cli(*args))
                try:
                    await asyncio.shield(creation)
                except asyncio.CancelledError:
                    try:
                        await creation
                        self._create_unknown = False
                    except OSError, TimeoutError, ValueError:
                        self._create_unknown = True
                    raise
                except OSError, TimeoutError, ValueError:
                    self._create_unknown = True
                    raise
                self._create_unknown = False
                self.valid()
                frame_cap = max(
                    self.template.ceiling.output_bytes * 2 + self.template.max_files * 1024 + 4096,
                    self.template.max_prompt_chars * 6 + 8192,
                )
                self.process = await asyncio.create_subprocess_exec(
                    "docker",
                    "start",
                    "--attach",
                    "--interactive",
                    self.name,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    limit=frame_cap + 1,
                )
                assert self.process.stdin is not None and self.process.stdout is not None
                initial = settings(
                    self.template, workspace=self.workspace, helper=self.helper, home=self.home
                )
                initial["warm_idle_seconds"] = self.idle_seconds
                if proxy is not None:
                    initial["proxy"] = proxy
                if self.resident:
                    initial["resident"] = True
                    # A margin past the host's own idle close: the host decides, not the timer.
                    initial["resident_idle_seconds"] = self.resident_idle_seconds + 60
                self.process.stdin.write(json.dumps(initial).encode() + b"\n")
                await self.process.stdin.drain()
                row = await self.process.stdout.readline()
                if row != b'{"kind":"warm_ready"}\n':
                    raise ValueError("WARM_PREPARATION_FAILED")
                self.valid()
                self.created_at = time.monotonic()
                self.ready = True
        except BaseException:
            await self.close()
            raise

    def claim(
        self,
        template: RunnerTemplate,
        task: RunnerTask,
        home_volume: str | None = None,
        internet: bool = False,
    ) -> bool:
        self.valid()
        if (
            home_volume != self.home_volume
            or internet != self.internet
            or self.closed
            or self.claimed
            or not self.ready
            or self.process is None
            or self.process.returncode is not None
            or template != self.template
            or task.limits != template.ceiling
            or task.network != "gateway"
            or self.idle(margin=5)
        ):
            return False
        template.check(task)
        self.claimed = True
        self.claimed_at = time.monotonic()
        return True

    def idle(self, margin: float = 0) -> bool:
        """Past the wait for the first turn, or (resident) past the idle time since the last."""
        now = time.monotonic()
        if self.turns == 0:
            return now - self.created_at >= self.idle_seconds - margin
        return now - self.released_at >= self.resident_idle_seconds - margin

    def release(self) -> bool:
        """After a clean turn a resident sandbox waits for the next; else the caller closes it."""
        if not self.resident or self.closed or not self.claimed:
            return False
        if self.process is None or self.process.returncode is not None:
            return False
        self.turns += 1
        self.released_at = time.monotonic()
        self.claimed = False
        return True

    def activate(self, template: RunnerTemplate, task: RunnerTask) -> asyncio.subprocess.Process:
        template.check(task)
        if (
            self.closed
            or not self.claimed
            or self.process is None
            or self.process.returncode is not None
            or template != self.template
            or task.limits != template.ceiling
            or task.network != "gateway"
            or time.monotonic() - self.claimed_at >= 5
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return self.process

    async def close(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self.ready = False
            # A real removal receipt, never merely an expired idle lease.
            if self._create_unknown:
                # Absence alone cannot settle a timed-out create. Require an actual
                # exact-name instance to remove, otherwise keep the resource unknown.
                try:
                    labels = json.loads(
                        await cli("inspect", "--format", "{{json .Config.Labels}}", self.name)
                    )
                    if labels.get("ai-agent-platform.instance") != self.name:
                        raise ValueError("WARM_INSTANCE_MISMATCH")
                except OSError, TimeoutError, ValueError, KeyError, IndexError:
                    await self.released(self.name, False)
                    raise ContractError(ErrorCode.OUTCOME_UNKNOWN) from None
            removed = await DockerTaskRunner._remove(self.name)
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            if self.gateway is not None:
                # After the container: the network is removable only without it.
                removed = await self.gateway.close() and removed
            if self._registered:
                await self.released(self.name, removed)
            if not removed:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            self.closed = True
