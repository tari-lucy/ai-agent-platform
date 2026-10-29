"""After a crash or a host reboot the platform's own leftover sandboxes are reclaimed.

A run that ended without removal receipts used to stay FAULTED for good (START closed). Now
the recovery checks Docker: gone or removed-now containers of this platform are confirmed;
foreign ids, mismatched labels and an unreachable Docker stay unknown.
"""

import asyncio
import json
import os
import subprocess
from uuid import uuid4

import pytest
from ai_agent_adapters.sandbox import docker


class FakeDocker:
    def __init__(self, containers=None, networks=(), broken=False):
        self.containers = dict(containers or {})  # name -> instance label
        self.networks, self.broken, self.calls = set(networks), broken, []
        self.attached: dict[str, tuple[str, ...]] = {}

    async def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if self.broken:
            raise OSError("docker unavailable")
        if args[:2] == ("ps", "--all"):
            label = args[3].split("=", 2)[2]
            return b"x\n" if any(v == label for v in self.containers.values()) else b""
        if args[0] == "inspect":
            if args[-1] not in self.containers:
                raise OSError("no such container")
            if "Networks" in args[2]:
                return json.dumps(dict.fromkeys(self.attached.get(args[-1], ()), {})).encode()
            return json.dumps({"ai-agent-platform.instance": self.containers[args[-1]]}).encode()
        if args[0] == "rm":
            self.containers.pop(args[-1])
            return b""
        if args[:2] == ("network", "ls"):
            net = args[3].removeprefix("name=^").removesuffix("$")
            return b"n\n" if net in self.networks else b""
        if args[:2] == ("network", "rm"):
            self.networks.discard(args[-1])
            return b""
        raise AssertionError(args)


def run(fake, resource_id, monkeypatch):
    monkeypatch.setattr(docker, "cli", fake)
    return asyncio.run(docker.reclaim(resource_id))


def test_gone_and_leftover_own_resources_are_confirmed(monkeypatch):
    hexid = "a" * 32
    assert run(FakeDocker(), "aw-task-" + hexid, monkeypatch)  # already gone
    left = FakeDocker({"aw-task-" + hexid: "aw-task-" + hexid})
    assert run(left, "warm-aw-task-" + hexid, monkeypatch) and not left.containers
    gateway = FakeDocker({"aw-egress-" + hexid: "aw-egress-" + hexid}, {"aw-net-" + hexid})
    assert run(gateway, "aw-egress-" + hexid, monkeypatch)
    assert not gateway.containers and not gateway.networks
    # A warm sandbox's own gateway is not registered: it is followed through its network.
    net = "e" * 32
    warm = FakeDocker(
        {"aw-task-" + hexid: "aw-task-" + hexid, "aw-egress-" + net: "aw-egress-" + net},
        {"aw-net-" + net},
    )
    warm.attached["aw-task-" + hexid] = ("aw-net-" + net,)
    assert run(warm, "warm-aw-task-" + hexid, monkeypatch)
    assert not warm.containers and not warm.networks


def test_foreign_ids_labels_and_broken_docker_stay_unknown(monkeypatch):
    hexid = "b" * 32
    assert not run(FakeDocker(), "workplace.files-writer", monkeypatch)  # not a sandbox id
    assert not run(FakeDocker(), "aw-task-xyz", monkeypatch)
    mismatch = FakeDocker({"aw-task-" + hexid: "aw-task-" + hexid})
    mismatch.containers["aw-task-" + hexid] = "aw-task-" + hexid
    original = mismatch.__call__

    async def other_label(*args, **kwargs):
        if args[0] == "inspect":
            return json.dumps({"ai-agent-platform.instance": "someone-else"}).encode()
        return await original(*args, **kwargs)

    assert not run(other_label, "aw-task-" + hexid, monkeypatch)
    assert not run(FakeDocker(broken=True), "aw-task-" + hexid, monkeypatch)


@pytest.mark.skipif(not os.getenv("AW_SANDBOX_IMAGE_ID"), reason="explicit image required")
def test_real_leftovers_of_a_rebooted_host_are_removed():
    """As on the rebooted Windows host: only the warm sandbox is an unconfirmed resource; its
    gateway container and internal network are left over too and go with it."""
    image = os.environ["AW_SANDBOX_IMAGE_ID"]
    hexid, gate = uuid4().hex, uuid4().hex
    task, egress, net = "aw-task-" + hexid, "aw-egress-" + gate, "aw-net-" + gate

    def docker_cli(*args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=True)

    docker_cli("network", "create", "--internal", net)
    for name in (task, egress):
        docker_cli("create", "--name", name, "--label", "ai-agent-platform.instance=" + name,
                   "--network=" + net, image)  # fmt: skip
    try:
        assert asyncio.run(docker.reclaim("warm-" + task))
        names = docker_cli("ps", "-a", "--format", "{{.Names}}").stdout.split()
        nets = docker_cli("network", "ls", "--format", "{{.Name}}").stdout.split()
        assert task not in names and egress not in names and net not in nets
    finally:
        subprocess.run(["docker", "rm", "-f", task, egress], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_recovery_keeps_only_what_docker_cannot_confirm(monkeypatch):
    from types import SimpleNamespace

    from ai_agent_core.m3_composition.policy import Readiness

    gone = {"aw-task-" + "c" * 32, "warm-aw-task-" + "d" * 32}

    async def fake_reclaim(resource_id):
        return resource_id in gone

    monkeypatch.setattr(docker, "reclaim", fake_reclaim)
    previous = SimpleNamespace(unconfirmed_resource_ids=[*sorted(gone), "external-effect-1"])
    unknown = asyncio.run(Readiness(SimpleNamespace()).reconcile(previous))
    assert unknown == ("external-effect-1",)
