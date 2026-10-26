"""Speculative empty one-shot sandbox, supervised independently of the model."""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ai_agent_adapters.sandbox.policy import Home, RunnerTemplate
from ai_agent_adapters.sandbox.warm import WarmSandbox
from ai_agent_application.agent_workplace.ports import RunnerTask
from ai_agent_application.control.ports import Admission, ResourceTicket
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState

if TYPE_CHECKING:
    from .service import Runtime


REFRESH_SECONDS = 5.0


class WarmSessions:
    def __init__(
        self,
        runtime: Runtime,
        template: RunnerTemplate,
        *,
        idle_seconds: float,
        helper: bool = False,
        home: Callable[[Scope], Home] | None = None,
    ) -> None:
        self.rt, self.template, self.idle_seconds = runtime, template, idle_seconds
        self.helper = helper
        # The active persona's home is mounted at creation; another home never claims it.
        self.home = home
        self.task: asyncio.Task[None] | None = None
        self.slot: WarmSandbox | None = None
        self.slots: set[WarmSandbox] = set()
        self.next_try = 0.0
        self.closed = False

    def tick(self) -> None:
        if self.closed or self.rt.supervisor.snapshot.state != RunState.ACTIVE:
            return
        if self.task is not None:
            if not self.task.done():
                return
            try:
                self.task.result()
            except ContractError, OSError, ValueError, TimeoutError:
                logging.getLogger(__name__).warning("OpenCode empty prewarm unavailable")
                self.next_try = time.monotonic() + 30
            self.task = None
        if time.monotonic() >= self.next_try:
            self.task = asyncio.create_task(self._prepare())

    async def _prepare(self) -> None:
        from .activity import tools

        rt = self.rt
        scope = rt.supervisor.snapshot.scope
        if not any(
            t.available and t.manifest.capability_id == self.template.id
            for t in await tools(rt, scope, session=True)
        ):
            self.next_try = time.monotonic() + REFRESH_SECONDS
            return
        admission = rt.supervisor.admit("observation")
        ticket: ResourceTicket | None = None

        def valid() -> None:
            nonlocal admission
            admission = rt.supervisor.resolve(
                admission.id, scope=admission.scope, freshness=admission.freshness
            )

        async def registered(name: str) -> None:
            nonlocal ticket
            valid()
            # Separate id from the eventual attempt's resource, both receipts required.
            ticket = rt.supervisor.register(admission, "warm-" + name)

        async def released(name: str, removed: bool) -> None:
            nonlocal ticket
            if ticket is not None:
                if not removed:
                    rt.supervisor.critical_failure(ErrorCode.OUTCOME_UNKNOWN)
                    return
                rt.supervisor.acknowledge(ticket)
                ticket = None
            try:
                valid()
            except ContractError:
                pass
            rt.supervisor.retire(admission)
            self.slots.discard(slot)

        slot = WarmSandbox(
            self.template,
            workspace=self.template.max_workspace_calls > 0,
            helper=self.helper,
            idle_seconds=self.idle_seconds,
            valid=valid,
            registered=registered,
            released=released,
            home=self.home(scope) if self.home is not None else None,
        )
        self.slot = slot
        self.slots.add(slot)
        boot = asyncio.create_task(slot.prepare())
        refresh_at = time.monotonic() + REFRESH_SECONDS
        refresh: asyncio.Task[Admission] | None = None
        try:
            while True:
                valid()
                if boot.done():
                    await boot
                    if slot.claimed or slot.closed:
                        break
                    if time.monotonic() - slot.created_at >= self.idle_seconds:
                        break
                if refresh is not None and refresh.done():
                    admission = refresh.result()
                    refresh = None
                    refresh_at = time.monotonic() + REFRESH_SECONDS
                if refresh is None and time.monotonic() >= refresh_at:
                    refresh = asyncio.create_task(rt.supervisor.refresh(admission))
                await asyncio.sleep(0.1)
        finally:
            if refresh is not None:
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)
            if not boot.done():
                boot.cancel()
            await asyncio.gather(boot, return_exceptions=True)
            if not slot.claimed:
                await slot.close()
                self.slots.discard(slot)
            if self.slot is slot:
                self.slot = None

    async def take(self, home_volume: str | None = None) -> WarmSandbox | None:
        slot = self.slot
        if slot is None:
            return None
        task = RunnerTask(
            self.template.id,
            self.template.executable_prefix,
            (),
            self.template.ceiling,
            network="gateway",
        )
        try:
            if slot.claim(self.template, task, home_volume):
                return slot
        except ContractError:
            pass
        # Do not run a cold 1 GiB sandbox beside a still preparing one.
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await slot.close()
        return None

    async def close(self) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for slot in tuple(self.slots):
            await slot.close()
        self.slots.clear()
