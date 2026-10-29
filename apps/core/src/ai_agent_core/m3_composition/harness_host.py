"""Long-lived harness of the active persona (doc33 stage 1, D79–D81).

START brings up the agent gateway and ``opencode serve`` in the persona's container; the
operator attaches to it directly. Nothing here wraps a turn: the harness runs its own loop.
The only binding to the run is its state and epoch: STOP (or any epoch change) closes the
gateway and both containers, and the run settles only on their removal receipts.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from ai_agent_adapters.sandbox.agent_gateway import AgentGateway, HarnessContainer
from ai_agent_adapters.sandbox.egress import Egress
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

if TYPE_CHECKING:
    from .harness_channel import ChannelPost
    from .harness_wakes import HarnessWakes
    from .service import Runtime

LOG = logging.getLogger(__name__)
# A call without reported usage is charged as if it were this large: unknown is never zero.
UNKNOWN_INPUT, UNKNOWN_OUTPUT = 32000, 4096
HTTP_STARTUP_TIMEOUT = 60.0


@dataclass(frozen=True)
class HarnessSettings:
    image_id: str
    port: int
    password: str
    upstream: str
    key: str
    model: str
    usage_dir: Path
    daily_budget: float
    input_rate: float | None  # per 1M tokens, budget currency; None = no tariff
    output_rate: float | None
    cached_rate: float | None = None  # per 1M cached input tokens; None = the input rate
    currency: str = "RUB"
    context_tokens: int = 128000
    output_tokens: int = 8192
    memory_mb: int = 4096
    pids: int = 1024
    rules: Path | None = None
    # The operator-facing name per persona id; a persona without one shows its id.
    agent_names: dict[str, str] = field(default_factory=dict)
    # Base of the workspaces on the host disk: <base>/<persona id> is /workspace.
    files_dir: Path | None = None
    # Operator-granted connections to host ports (name -> port), e.g. a game mod's server.
    host_ports: dict[str, int] = field(default_factory=dict)


class Usage:
    """Local daily ledger of the harness's model calls; possibly personal, never in Git."""

    def __init__(self, settings: HarnessSettings) -> None:
        self.settings = settings

    def _path(self) -> Path:
        return self.settings.usage_dir / f"harness-{datetime.now(UTC).date().isoformat()}.jsonl"

    def cost(self, row: dict[str, Any]) -> float:
        s = self.settings
        assert s.input_rate is not None and s.output_rate is not None
        reported_in, reported_out = row.get("input"), row.get("output")
        tokens_in = reported_in if isinstance(reported_in, int) else UNKNOWN_INPUT
        tokens_out = reported_out if isinstance(reported_out, int) else UNKNOWN_OUTPUT
        reported_cached = row.get("cached")
        cached = (
            min(reported_cached, tokens_in)
            if isinstance(reported_cached, int) and isinstance(reported_in, int)
            else 0
        )
        cached_rate = s.input_rate if s.cached_rate is None else s.cached_rate
        return (
            (tokens_in - cached) * s.input_rate + cached * cached_rate + tokens_out * s.output_rate
        ) / 1_000_000

    def record(self, row: dict[str, Any]) -> None:
        if row.get("kind") == "refused":
            LOG.warning("Harness model call refused: %s", row.get("code"))
            return
        if row.get("kind") != "usage" or self.settings.input_rate is None:
            return
        if row.get("status") != 200 and row.get("input") is None:
            return  # the provider refused before any work: nothing billed
        entry = {
            "at": datetime.now(UTC).isoformat(),
            "input": row.get("input"),
            "output": row.get("output"),
            "cached": row.get("cached"),
            "cost": round(self.cost(row), 6),
        }
        self.settings.usage_dir.mkdir(parents=True, exist_ok=True)
        with self._path().open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")

    def calls(self) -> int:
        try:
            return sum(1 for line in self._path().read_text(encoding="utf-8").splitlines() if line)
        except OSError:
            return 0

    def spent(self) -> float:
        try:
            lines = self._path().read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0.0
        total = 0.0
        for line in lines:
            try:
                total += float(json.loads(line)["cost"])
            except ValueError, KeyError, TypeError:
                continue
        return total

    def remaining(self) -> float | None:
        if self.settings.input_rate is None or self.settings.output_rate is None:
            return None
        return self.settings.daily_budget - self.spent()


class HarnessPart(Protocol):
    """A platform owner working beside the harness (stage 3: learning, autonomy).

    ``tick`` is called about every 0.5 s, only while the harness is ready in the epoch it
    started with; it must not block (schedule work as tasks) and checks ``host.current()``
    again before any effect. ``close`` is awaited before the containers go, on STOP, an
    epoch change or a failure; a later START ticks the same part again.
    """

    def tick(self) -> None: ...

    async def close(self) -> None: ...


class HarnessHost:
    def __init__(
        self,
        runtime: Runtime,
        settings: HarnessSettings,
        *,
        home: Callable[[], str],
        prompt: Callable[[], Awaitable[str]],
        internet: Callable[[], Awaitable[Callable[[], Egress] | None]] | None = None,
    ) -> None:
        self.rt, self.settings = runtime, settings
        self.home, self.prompt, self.internet = home, prompt, internet
        self.usage = Usage(settings)
        self.task: asyncio.Task[None] | None = None
        self.next_try = 0.0
        self.ready = False
        self.closed = False
        self.epoch: int | None = None
        self.parts: list[HarnessPart] = []
        # The agent's own alarms (stage 3); set by the host wiring, read by autonomy.
        self.wakes: HarnessWakes | None = None
        self.channel: ChannelPost | None = None

    def tick(self) -> None:
        if self.closed or self.rt.supervisor.snapshot.state != RunState.ACTIVE:
            return
        if self.task is not None:
            if not self.task.done():
                return
            try:
                self.task.result()
            except ContractError, OSError, ValueError, TimeoutError:
                LOG.warning("Harness unavailable, retrying in 30 s", exc_info=True)
                self.next_try = time.monotonic() + 30
            self.task = None
        if time.monotonic() >= self.next_try:
            self.task = asyncio.create_task(self._run())

    def attach(self, part: HarnessPart) -> None:
        self.parts.append(part)

    def current(self) -> bool:
        """Ready, ACTIVE and still in the epoch this harness started with."""
        run = self.rt.supervisor.snapshot
        return self.ready and run.state == RunState.ACTIVE and run.control_epoch == self.epoch

    def persona(self) -> str:
        return self.rt.supervisor.snapshot.scope.persona_id

    def agent_name(self) -> str:
        return self.settings.agent_names.get(self.persona(), self.persona())

    def folders(self) -> Path | None:
        """Each persona has its own workspace folder; another persona never sees it."""
        if self.settings.files_dir is None:
            return None
        return self.settings.files_dir / self.persona()

    def config(self, model_url: str, prompt: str, internet: bool) -> dict[str, Any]:
        s = self.settings
        name = self.agent_name()
        if s.host_ports:
            host = model_url.split("//", 1)[1].split(":", 1)[0]
            prompt += "\n\n## Подключения к компьютеру оператора\n" + "\n".join(
                f"- {label}: {host}:{port} (выдано оператором; другого пути к его компьютеру и "
                "сети нет, перебирать адреса не нужно)"
                for label, port in sorted(s.host_ports.items())
            )
        free = {
            "bash": "allow",
            "edit": "allow",
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "list": "allow",
            "todowrite": "allow",
            "todoread": "allow",
            "task": "allow",
            # The container is the boundary (D70/D72); paths outside /workspace are its own.
            "external_directory": "allow",
            "webfetch": "allow" if internet else "deny",
            "platform_*": "allow",
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "model": "platform/bound",
            "small_model": "platform/bound",
            "enabled_providers": ["platform"],
            "autoupdate": False,
            "share": "disabled",
            "provider": {
                "platform": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Platform",
                    # Not a key: the gateway holds the real one and ignores this marker.
                    "options": {"baseURL": model_url, "apiKey": "platform-gateway"},
                    "models": {
                        "bound": {
                            "name": s.model,
                            "limit": {"context": s.context_tokens, "output": s.output_tokens},
                            "tool_call": True,
                        }
                    },
                }
            },
            # Platform MCP (stage 2): memory, persona, tickets, relayed by the agent gateway.
            "mcp": {
                "platform": {
                    "type": "remote",
                    "url": model_url.rsplit("/v1", 1)[0] + "/mcp",
                    "enabled": True,
                }
            },
            "default_agent": name,
            "agent": {
                name: {
                    "mode": "primary",
                    "description": "Личность в своём рабочем месте",
                    "prompt": prompt,
                    "permission": free,
                },
            },
            "permission": free,
        }

    async def _run(self) -> None:
        rt, s = self.rt, self.settings
        epoch = rt.supervisor.snapshot.control_epoch
        admission = rt.supervisor.admit("observation")
        ticket = rt.supervisor.register(admission, "harness-" + uuid4().hex)
        from .harness_mcp import PlatformMcp

        self.epoch = epoch
        gateway = AgentGateway(
            s.image_id,
            s.port,
            self.usage.record,
            PlatformMcp(rt, lambda: self.epoch, self.wakes, self.channel).handle,
            tuple(s.host_ports.values()),
        )
        folder = self.folders()
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
        agent = HarnessContainer(
            s.image_id,
            self.home(),
            memory_mb=s.memory_mb,
            pids=s.pids,
            workspace=None if folder is None else str(folder.resolve()),
        )
        egress: Egress | None = None
        try:
            prompt = await self.prompt()
            factory = await self.internet() if self.internet is not None else None
            proxy: dict[str, str] = {}
            if factory is not None:
                egress = factory()
                url = await egress.open()
                network = egress.network
                proxy = {key: url for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                                              "https_proxy")}  # fmt: skip
            else:
                network = await agent.network_open()
            model_url = await gateway.open(
                network,
                upstream=s.upstream,
                key=s.key,
                model=s.model,
                remaining=self.usage.remaining(),
            )
            host = model_url.split("//", 1)[1].split(":", 1)[0]
            proxy.update({"NO_PROXY": host, "no_proxy": host})
            address = await agent.open(
                network,
                password=s.password,
                config=self.config(model_url, prompt, factory is not None),
                proxy=proxy,
            )
            gateway.agent(address)
            await self._wait_ready(epoch)
            self.ready = True
            LOG.warning("Harness ready: opencode attach http://127.0.0.1:%d", s.port)
            budget_at = 0.0
            while True:
                run = rt.supervisor.snapshot
                if run.state != RunState.ACTIVE or run.control_epoch != epoch:
                    break
                if not gateway.alive or not agent.alive:
                    raise OSError("HARNESS_EXITED")
                if time.monotonic() >= budget_at:
                    gateway.budget(self.usage.remaining())
                    budget_at = time.monotonic() + 5
                for part in self.parts:
                    try:
                        part.tick()
                    except Exception:  # noqa: BLE001 - one part never stops the harness
                        LOG.warning("Harness part tick failed", exc_info=True)
                await asyncio.sleep(0.5)
        finally:
            self.ready = False
            for part in self.parts:
                try:
                    await asyncio.wait_for(part.close(), 10)
                except Exception:  # noqa: BLE001 - containers close regardless
                    LOG.warning("Harness part close failed", exc_info=True)
            gateway.stop()
            removed = await agent.close()
            removed = await gateway.close() and removed
            removed = await agent.close_network() and removed
            if egress is not None:
                removed = await egress.close() and removed
            if removed:
                rt.supervisor.acknowledge(ticket)
            else:
                rt.supervisor.critical_failure(ErrorCode.OUTCOME_UNKNOWN)
            try:
                rt.supervisor.retire(admission)
            except ContractError:
                pass
            LOG.warning("Harness closed (removed=%s)", removed)

    async def opencode(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 60
    ) -> Any:
        """The running harness's own OpenCode API (sessions, messages), for platform owners.

        Stage 3 (learning, autonomy) reads and writes conversations only through this: the
        harness owns them. Refused unless the harness is ready in an ACTIVE run.
        """
        if not self.current():
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if method not in ("GET", "POST") or not path.startswith("/"):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        return await self._request_opencode(method, path, body, timeout=timeout)

    async def _wait_ready(self, epoch: int) -> None:
        """A Docker address is not yet a listening OpenCode HTTP server."""
        deadline = time.monotonic() + HTTP_STARTUP_TIMEOUT

        def guard() -> None:
            run = self.rt.supervisor.snapshot
            if self.closed or run.state != RunState.ACTIVE or run.control_epoch != epoch:
                raise ContractError(ErrorCode.STALE_CONTEXT)

        while time.monotonic() < deadline:
            guard()
            try:
                health = await asyncio.wait_for(
                    self._request_opencode("GET", "/global/health", timeout=2),
                    timeout=min(2.0, max(0.001, deadline - time.monotonic())),
                )
            except (OSError, ValueError, TimeoutError):
                health = None
            guard()
            if isinstance(health, dict) and health.get("healthy") is True:
                return
            await asyncio.sleep(0.25)
        raise OSError("HARNESS_HTTP_NOT_READY")

    async def _request_opencode(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float
    ) -> Any:
        import base64
        import urllib.request

        auth = base64.b64encode(f"opencode:{self.settings.password}".encode()).decode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.settings.port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Basic " + auth},
            method=method,
        )

        def call() -> Any:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(64 * 1024 * 1024)
            return json.loads(raw) if raw else None

        return await asyncio.to_thread(call)

    async def close(self) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
