"""Only trusted templates select immutable images and absolute executables."""

import re
from dataclasses import dataclass

from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode


@dataclass(frozen=True)
class RunnerTemplate:
    id: str
    image_id: str
    executable_prefix: tuple[str, ...]
    ceiling: RunnerLimits
    max_input_bytes: int
    max_files: int = 32
    model_role_id: str | None = None
    max_model_calls: int = 4
    max_prompt_chars: int = 16000
    max_model_tokens: int = 512

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.id) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_id) is None
            or not self.executable_prefix
            or not self.executable_prefix[0].startswith("/")
            or type(self.max_input_bytes) is not int
            or self.max_input_bytes < 1
            or type(self.max_files) is not int
            or not 1 <= self.max_files <= 200
            or self.model_role_id is not None
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.model_role_id) is None
            or type(self.max_model_calls) is not int
            or not 1 <= self.max_model_calls <= 16
            or type(self.max_prompt_chars) is not int
            or not 1 <= self.max_prompt_chars <= 100000
            or type(self.max_model_tokens) is not int
            or not 1 <= self.max_model_tokens <= 65536
        ):
            raise ValueError("RUNNER_TEMPLATE_INVALID")
        limits(self.ceiling)

    def check(self, task: RunnerTask) -> None:
        limits(task.limits)
        if (
            task.template_id != self.id
            or not self.executable_prefix == task.command[: len(self.executable_prefix)]
            or not len(self.executable_prefix) <= len(task.command) <= 64
            or any(
                not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in task.command
            )
            or sum(len(arg) for arg in task.command) > 24000
            or len(task.inputs) > self.max_files
        ):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        for name in RunnerLimits.__dataclass_fields__:
            if getattr(task.limits, name) > getattr(self.ceiling, name):
                raise ContractError(ErrorCode.ACCESS_DENIED)
        # Gateway is stdio RPC to one bound model role, never a Docker network.
        if (
            task.network not in {"none", "gateway"}
            or task.network == "gateway"
            and self.model_role_id is None
        ):
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)


def limits(value: RunnerLimits) -> None:
    if (
        any(
            type(getattr(value, field)) is not int or getattr(value, field) < 1
            for field in RunnerLimits.__dataclass_fields__
        )
        or value.pids < 4
        or value.memory_mb < 32
        or value.disk_bytes < 4096
    ):
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)


def create_args(template: RunnerTemplate, task: RunnerTask, name: str, entry: str) -> list[str]:
    """No host mounts/ports/keys/socket. Writable task disk is a kernel-bounded tmpfs."""
    template.check(task)
    if re.fullmatch(r"aw-task-[0-9a-f]{32}", name) is None:
        raise ValueError("RUNNER_NAME_INVALID")
    cap = task.limits
    return [
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        "ai-agent-platform.runner=aw-04",
        "--interactive",
        "--network=none",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--ipc=none",
        "--log-driver=none",
        "--no-healthcheck",
        "--restart=no",
        "--pids-limit",
        str(cap.pids),
        "--cpus=1",
        "--memory",
        f"{cap.memory_mb}m",
        "--memory-swap",
        f"{cap.memory_mb}m",
        "--ulimit",
        f"cpu={cap.cpu_seconds}:{cap.cpu_seconds}",
        "--ulimit",
        "nofile=128:128",
        "--ulimit",
        "core=0:0",
        "--tmpfs",
        f"/work:rw,nosuid,nodev,noexec,size={cap.disk_bytes},nr_inodes=2048,mode=0700,uid=65532,gid=65532",
        "--workdir=/work",
        "--entrypoint=/usr/local/bin/python",
        template.image_id,
        "-I",
        "-u",
        "-c",
        entry,
    ]
