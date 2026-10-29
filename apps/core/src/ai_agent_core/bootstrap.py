"""Validated stopped bootstrap, with one control owner and no import-time IO."""

import asyncio
import json
import logging
import sys
import threading
import time
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from importlib.metadata import version
from typing import Any, Protocol

from ai_agent_adapters.configuration import (
    BootstrapSettings,
    ConfigurationError,
    fresh_startup,
    from_environment,
    validate_bindings,
)
from ai_agent_adapters.control.journal import UnavailableJournal, UnavailableReadiness
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Clock
from ai_agent_application.scheduling import ResourceScheduler, SchedulerLimits
from ai_agent_application.scheduling.model import DEFAULT_POOLS
from ai_agent_application.telemetry import HealthState, Telemetry
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from fastapi import FastAPI

from .access import AccessPorts, install_access

LOG = logging.getLogger(__name__)
STALL_SECONDS = 1.0


def where(frames: list[traceback.FrameSummary], depth: int = 6) -> str:
    """Code locations only (file:function:line): no values, messages or paths of the host."""
    return " < ".join(
        f"{f.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{f.name}:{f.lineno}"
        for f in reversed(frames[-depth:])
    )


SLOW_REQUEST_SECONDS = 0.5


class SlowRequests:
    """ASGI wrapper: logs HTTP requests whose response headers take >= SLOW_REQUEST_SECONDS.

    Method and path only (ids, no query, no body); streams count until their first byte.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        reported = False

        async def timed(message: Any) -> None:
            nonlocal reported
            if message.get("type") == "http.response.start" and not reported:
                reported = True
                spent = time.monotonic() - started
                if spent >= SLOW_REQUEST_SECONDS:
                    LOG.warning(
                        "Slow request %.2fs %s %s -> %s",
                        spent,
                        scope.get("method", ""),
                        scope.get("path", "")[:200],
                        message.get("status"),
                    )
            await send(message)

        await self.app(scope, receive, timed)


class LoopStall:
    """A watcher thread: when the event loop has not run for STALL_SECONDS, log once where
    its thread stands. A blocked loop delays every timer, and a gap past the supervisor's
    sleep limit faults the run; this names the blocking code, never its data."""

    def __init__(self, threshold: float = STALL_SECONDS) -> None:
        self.threshold, self.beat = threshold, time.monotonic()
        self.loop_thread = threading.get_ident()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, name="loop-stall", daemon=True)

    def start(self) -> None:
        self.loop_thread = threading.get_ident()
        self.beat = time.monotonic()
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def _watch(self) -> None:
        reported = 0.0
        while not self.stopped.wait(0.25):
            lag = time.monotonic() - self.beat
            if lag >= self.threshold and not reported:
                frame = sys._current_frames().get(self.loop_thread)
                stack = traceback.extract_stack(frame) if frame is not None else []
                LOG.warning("Event loop blocked %.1fs at %s", lag, where(stack, 8))
                reported = lag
            elif lag < self.threshold and reported:
                reported = 0.0


class ManagedRuntime(Protocol):
    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def tick(self) -> None: ...
    async def wait_for_work(self, seconds: float = 1.0) -> None: ...
    def access_ports(self) -> AccessPorts: ...


def create_app(
    settings: BootstrapSettings | None = None,
    *,
    previous_state: str | None = None,
    base_scope: Scope | None = None,
    runtime_factory: Callable[[Supervisor, ResourceScheduler], ManagedRuntime] | None = None,
    clock: Clock | None = None,
    access_ports: AccessPorts | None = None,
    access_factory: Callable[[Supervisor], AccessPorts] | None = None,
) -> FastAPI:
    """Construct a stopped supervisor with fail-closed authenticated access."""
    configuration = from_environment() if settings is None else settings
    validate_bindings(configuration)
    startup = fresh_startup(previous_state)
    if base_scope is not None and (
        base_scope.workspace_id != configuration.workspace_id
        or base_scope.persona_id != configuration.persona_id
        or base_scope.memory_mode != configuration.memory_scope
        or base_scope.agent_id is not None
    ):
        raise ConfigurationError("runtime", "scope_mismatch")
    supervisor = Supervisor(
        base_scope
        or Scope(
            workspace_id=configuration.workspace_id,
            persona_id=configuration.persona_id,
            memory_space_id=configuration.memory_scope + "-bootstrap",
            memory_mode=configuration.memory_scope,
        ),
        "primary",
        UnavailableJournal(),
        UnavailableReadiness(),
        clock=clock,
    )

    telemetry = Telemetry(clock=supervisor.clock, critical_failure=supervisor.critical_failure)
    telemetry.register("control")
    telemetry.update_health("control", HealthState.READY)
    pools = tuple(
        replace(pool, capacity=configuration.worker_memory_mib) if pool.id == "ram" else pool
        for pool in DEFAULT_POOLS
    )
    scheduler = ResourceScheduler(
        supervisor,
        telemetry,
        pools=pools,
        limits=SchedulerLimits(pending=configuration.pending_jobs),
    )

    runtime = None if runtime_factory is None else runtime_factory(supervisor, scheduler)
    if runtime is not None and (access_ports is not None or access_factory is not None):
        raise ConfigurationError("access", "single_composition_required")
    if runtime is not None:
        access_ports = runtime.access_ports()

    async def runtime_watchdog() -> None:
        while True:
            try:
                if runtime is not None:
                    await runtime.tick()
            except Exception as error:
                # Type, fixed code and code location only: messages may carry data.
                code = error.error.code.value if isinstance(error, ContractError) else "-"
                LOG.error(
                    "Runtime tick failed: %s %s at %s",
                    type(error).__name__,
                    code,
                    where(traceback.extract_tb(error.__traceback__)),
                )
                supervisor.critical_failure()
            if runtime is None:
                await asyncio.sleep(1)
            else:
                await runtime.wait_for_work()

    stall = LoopStall()

    async def operational_watchdog() -> None:
        while True:
            stall.beat = time.monotonic()
            scheduler.poll()
            telemetry.update_health("control", HealthState.READY)
            await asyncio.sleep(0.05)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime is not None:
            await runtime.open()
        stall.start()
        worker = asyncio.create_task(runtime_watchdog())
        watchdog = asyncio.create_task(supervisor.watchdog())
        operations = asyncio.create_task(operational_watchdog())
        try:
            yield
        finally:
            stall.stop()
            access.sessions.shutdown()
            supervisor.shutdown()
            scheduler.poll()
            worker.cancel()
            watchdog.cancel()
            operations.cancel()
            for task in (worker, watchdog, operations):
                with suppress(asyncio.CancelledError):
                    await task
            if runtime is not None:
                await runtime.close()

    app = FastAPI(
        title="AI Agent Platform",
        version=version("ai-agent-platform"),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(SlowRequests)
    app.state.bootstrap = startup
    app.state.supervisor = supervisor
    app.state.telemetry = telemetry
    app.state.scheduler = scheduler
    app.state.runtime = runtime
    # Credentials are not attached to API state or returned via a route.
    app.state.bootstrap_manifest = startup.safe_manifest(configuration)
    if access_factory is not None:
        if access_ports is not None:
            raise ConfigurationError("access", "single_composition_required")
        access_ports = access_factory(supervisor)
    access = install_access(app, supervisor, configuration, access_ports)
    return app


def main() -> None:
    """Safe configuration check; no web listener or active agent."""
    try:
        app = create_app()
    except ConfigurationError as error:
        print(str(error))
        raise SystemExit(78) from None
    print(json.dumps(app.state.bootstrap_manifest, sort_keys=True))
