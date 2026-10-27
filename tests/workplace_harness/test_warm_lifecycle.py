"""No Docker: delayed readiness cannot block STOP; cold fallback disposes preparation."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from ai_agent_adapters.agent_harness.opencode import PREFIX
from ai_agent_adapters.sandbox.policy import RunnerTemplate
from ai_agent_adapters.sandbox.warm import WarmSandbox
from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition import activity, work_session_warm

CAP = RunnerLimits(30, 1024, 128, 60, 65536, 32 * 1024 * 1024)
TEMPLATE = RunnerTemplate(
    "fixture", "sha256:" + "a" * 64, PREFIX, CAP, 65536, model_role_id="activity"
)


class Control:
    def __init__(self):
        self.snapshot = SimpleNamespace(state=RunState.ACTIVE, scope="scope")
        self.current = SimpleNamespace(id="admission", scope="scope", freshness="fresh")
        self.alive = True
        self.registered, self.removed = [], []
        self.refreshing, self.refresh_release = asyncio.Event(), asyncio.Event()

    def admit(self, kind):
        assert kind == "observation"
        return self.current

    def resolve(self, identity, **kwargs):
        if not self.alive:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return self.current

    def register(self, admission, name):
        self.registered.append(name)
        return name

    def acknowledge(self, ticket):
        self.removed.append(ticket)

    def retire(self, admission):
        pass

    def critical_failure(self, error):
        raise AssertionError(error)

    async def refresh(self, admission):
        self.refreshing.set()
        await self.refresh_release.wait()
        self.current = SimpleNamespace(id=admission.id, scope="scope", freshness="fresh")
        return self.current


class EmptySlot:
    block_boot = False

    def __init__(self, template, **options):
        self.template, self.options = template, options
        self.name, self.claimed, self.ready, self.closed = "fake", False, False, False
        self.internet = False
        self.created_at = time.monotonic()
        self.booting = asyncio.Event()

    async def prepare(self):
        await self.options["registered"](self.name)
        self.booting.set()
        if self.block_boot:
            await asyncio.Event().wait()
        self.ready = True

    def claim(self, template, task, home_volume=None, internet=False):
        self.options["valid"]()
        if not self.ready:
            return False
        self.claimed = True
        return True

    def idle(self, margin=0):
        return time.monotonic() - self.created_at >= self.options["idle_seconds"] - margin

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.options["released"](self.name, True)


async def setup(monkeypatch, *, block=False, **options):
    control = Control()
    rt = SimpleNamespace(supervisor=control)

    async def tools(*args, **kwargs):
        return [SimpleNamespace(available=True, manifest=SimpleNamespace(capability_id="fixture"))]

    monkeypatch.setattr(activity, "tools", tools)
    monkeypatch.setattr(work_session_warm, "WarmSandbox", EmptySlot)
    monkeypatch.setattr(EmptySlot, "block_boot", block)
    manager = work_session_warm.WarmSessions(rt, TEMPLATE, idle_seconds=30, **options)
    manager.tick()
    while manager.slot is None:
        await asyncio.sleep(0)
    await manager.slot.booting.wait()
    return control, manager


def test_stop_cancels_slow_refresh_without_waiting_for_its_readiness(monkeypatch):
    async def scenario():
        monkeypatch.setattr(work_session_warm, "REFRESH_SECONDS", 0.01)
        control, manager = await setup(monkeypatch)
        try:
            await asyncio.wait_for(control.refreshing.wait(), timeout=1)
            slot = manager.slot
            control.alive = False
            await asyncio.wait_for(asyncio.gather(manager.task, return_exceptions=True), timeout=1)
            assert slot.closed and control.removed == control.registered
            assert not control.refresh_release.is_set()
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_successful_refresh_and_claim_use_current_admission(monkeypatch):
    async def scenario():
        monkeypatch.setattr(work_session_warm, "REFRESH_SECONDS", 0.01)
        control, manager = await setup(monkeypatch)
        try:
            await asyncio.wait_for(control.refreshing.wait(), timeout=1)
            old = control.current
            control.refresh_release.set()
            await asyncio.sleep(0.15)
            assert control.current is not old
            slot = await manager.take()
            assert slot is not None and slot.claimed
            assert not slot.closed
        finally:
            await manager.close()
        assert control.removed == control.registered

    asyncio.run(scenario())


def test_resident_slot_outlives_its_turns_until_idle_or_stop(monkeypatch):
    async def scenario():
        control, manager = await setup(monkeypatch, resident=True, resident_idle_seconds=60)
        try:
            await asyncio.sleep(0.15)
            slot = await manager.take()
            assert slot is not None and slot.claimed
            slot.claimed = False  # the runner released it after a clean turn
            await asyncio.sleep(0.2)
            assert not slot.closed and not manager.task.done()  # kept for the next turn
            assert await manager.take() is slot
            slot.claimed = False
            slot.created_at = time.monotonic() - 3600  # now idle
            await asyncio.wait_for(asyncio.gather(manager.task, return_exceptions=True), 2)
            assert slot.closed and control.removed == control.registered
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_cold_fallback_cleans_unready_slot_before_returning(monkeypatch):
    async def scenario():
        control, manager = await setup(monkeypatch, block=True)
        slot = manager.slot
        try:
            assert await manager.take() is None
            assert slot.closed and control.removed == control.registered
            assert manager.task is None
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_claim_and_activation_require_live_process_time_and_actual_task():
    async def scenario():
        async def unused(*args):
            pass

        slot = WarmSandbox(
            TEMPLATE,
            workspace=False,
            helper=False,
            idle_seconds=30,
            valid=lambda: None,
            registered=unused,
            released=unused,
        )
        slot.ready = True
        task = RunnerTask(TEMPLATE.id, PREFIX, (), CAP, network="gateway")
        slot.process = SimpleNamespace(returncode=0)
        assert not slot.claim(TEMPLATE, task)
        slot.process = SimpleNamespace(returncode=None)
        slot.created_at -= 26
        assert not slot.claim(TEMPLATE, task)
        slot.created_at = time.monotonic()
        assert slot.claim(TEMPLATE, task)
        assert slot.activate(TEMPLATE, task) is slot.process
        for changed in (
            replace(task, limits=replace(CAP, memory_mb=512)),
            replace(task, network="none"),
        ):
            with pytest.raises(ContractError):
                slot.activate(TEMPLATE, changed)
        slot.claimed_at -= 6
        with pytest.raises(ContractError):
            slot.activate(TEMPLATE, task)

    asyncio.run(scenario())


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancel_waits_for_late_create_before_removal_receipt(monkeypatch, repeat_cancel):
    async def scenario():
        from ai_agent_adapters.sandbox import warm
        from ai_agent_adapters.sandbox.docker import DockerTaskRunner

        started, finish = asyncio.Event(), asyncio.Event()
        trace = []

        async def image(*args):
            pass

        async def command(*args, **kwargs):
            if args[0] == "inspect":
                raise OSError("not yet visible")
            assert args[0] == "create"
            trace.append("create-start")
            started.set()
            await finish.wait()
            trace.append("create-finished")
            return b"synthetic-id"

        async def remove(name):
            trace.append("remove")
            return True

        async def register(name):
            trace.append("register")

        async def receipt(name, success):
            trace.append("receipt" if success else "unknown")

        monkeypatch.setattr(DockerTaskRunner, "_image", image)
        monkeypatch.setattr(DockerTaskRunner, "_remove", remove)
        monkeypatch.setattr(warm, "cli", command)
        slot = WarmSandbox(
            TEMPLATE,
            workspace=False,
            helper=False,
            idle_seconds=30,
            valid=lambda: None,
            registered=register,
            released=receipt,
        )
        boot = asyncio.create_task(slot.prepare())
        await started.wait()
        boot.cancel()
        await asyncio.sleep(0.02)
        assert trace == ["register", "create-start"]
        if repeat_cancel:
            boot.cancel()
            with pytest.raises(ContractError) as caught:
                await boot
            assert caught.value.error.code == ErrorCode.OUTCOME_UNKNOWN
            assert trace == ["register", "create-start", "unknown"]
            assert not slot.closed
        else:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await boot
            assert trace == ["register", "create-start", "create-finished", "remove", "receipt"]
            assert slot.closed

    asyncio.run(scenario())


def test_unknown_create_cannot_be_settled_by_initial_absence(monkeypatch):
    async def scenario():
        from ai_agent_adapters.sandbox import warm
        from ai_agent_adapters.sandbox.docker import DockerTaskRunner

        receipts = []

        async def image(*args):
            pass

        async def command(*args, **kwargs):
            raise TimeoutError() if args[0] == "create" else OSError()

        async def remove(name):
            raise AssertionError("absence must not issue a removal receipt")

        async def register(name):
            pass

        async def receipt(name, success):
            receipts.append(success)

        monkeypatch.setattr(DockerTaskRunner, "_image", image)
        monkeypatch.setattr(DockerTaskRunner, "_remove", remove)
        monkeypatch.setattr(warm, "cli", command)
        slot = WarmSandbox(
            TEMPLATE,
            workspace=False,
            helper=False,
            idle_seconds=30,
            valid=lambda: None,
            registered=register,
            released=receipt,
        )
        with pytest.raises(ContractError) as caught:
            await slot.prepare()
        assert caught.value.error.code == ErrorCode.OUTCOME_UNKNOWN
        assert receipts == [False] and not slot.closed

    asyncio.run(scenario())


def test_unknown_create_still_removes_the_sandboxs_own_gateway(monkeypatch):
    """A refused create (e.g. Windows command line too long) must not leave proxy/network."""
    from ai_agent_adapters.sandbox import warm
    from ai_agent_adapters.sandbox.docker import DockerTaskRunner

    class Gateway:
        network, closed = "aw-net-" + "c" * 32, 0

        async def open(self):
            return "http://10.0.0.2:3128"

        async def close(self):
            self.closed += 1
            return True

    async def cli(*args, **kwargs):
        raise OSError("create refused")

    async def image(*args, **kwargs):
        return None

    async def scenario():
        gateway, released = Gateway(), []

        async def registered(name):
            return None

        async def note(name, removed):
            released.append(removed)

        monkeypatch.setattr(warm, "cli", cli)
        monkeypatch.setattr(DockerTaskRunner, "_image", staticmethod(image))
        slot = WarmSandbox(
            TEMPLATE, workspace=True, helper=False, idle_seconds=30, valid=lambda: None,
            registered=registered, released=note, egress=lambda: gateway,
        )  # fmt: skip
        with pytest.raises(ContractError) as error:
            await slot.prepare()
        assert error.value.error.code is ErrorCode.OUTCOME_UNKNOWN
        assert gateway.closed == 1 and released == [False]

    asyncio.run(scenario())
