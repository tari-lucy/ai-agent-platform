"""No daemon needed: model output cannot select image, mounts or Docker arguments."""

from dataclasses import replace

import pytest
from ai_agent_adapters.sandbox.policy import RunnerTemplate, create_args
from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask
from ai_agent_contracts.codec import ContractError

CAP = RunnerLimits(2, 64, 8, 3, 1024, 1024 * 1024)
TEMPLATE = RunnerTemplate(
    "python", "sha256:" + "a" * 64, ("/usr/local/bin/python", "-I"), CAP, 4096
)
TASK = RunnerTask("python", ("/usr/local/bin/python", "-I", "-c", "print('test')"), (), CAP)


@pytest.mark.parametrize(
    "change",
    [
        {"template_id": "unknown"},
        {"network": "gateway"},
        {"command": ("/bin/sh", "-c", "anything")},
        {"limits": replace(CAP, memory_mb=128)},
        {"limits": replace(CAP, disk_bytes=True)},
    ],
)
def test_model_cannot_extend_template(change):
    with pytest.raises(ContractError):
        TEMPLATE.check(replace(TASK, **change))


def test_no_mount_socket_ports_or_secret_arguments():
    args = create_args(TEMPLATE, TASK, "aw-task-" + "b" * 32, "trusted entry")
    assert "--network=none" in args and "--read-only" in args and "--cap-drop=ALL" in args
    assert not any(
        arg in {"--mount", "--volume", "--publish", "--env-file", "--privileged"} for arg in args
    )
    assert not any("docker.sock" in arg for arg in args)
    assert "size=1048576" in next(arg for arg in args if arg.startswith("/work:"))
    with pytest.raises(ValueError):
        replace(TEMPLATE, image_id="python:latest")
