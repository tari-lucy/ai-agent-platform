"""Only trusted templates select immutable images and absolute executables."""

import base64
import re
import zlib
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
    max_workspace_calls: int = 0
    max_helper_calls: int = 0
    monitor_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.id) is None
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
            or type(self.max_workspace_calls) is not int
            or not 0 <= self.max_workspace_calls <= 64
            or self.max_workspace_calls > 0
            and self.model_role_id is None
            or type(self.max_helper_calls) is not int
            or not 0 <= self.max_helper_calls <= 16
            or self.max_helper_calls > 0
            and self.model_role_id is None
            or type(self.monitor_interval_seconds) not in (float, int)
            or not 0.1 <= self.monitor_interval_seconds <= 1.0
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


# The persistent home of one persona partition: a named Docker volume, never a host path.
HOME_VOLUME = re.compile(r"aw-home-[0-9a-f]{32}")
GUIDE_BYTES = 32768


@dataclass(frozen=True)
class Home:
    """Trusted per-attempt home: the volume and the read-only guide written for this turn.

    ``rules`` is the operator's guide, ``connections`` the platform's facts and ``readme``
    the starter README for an empty home. Text only; none of it grants a right.
    A Docker volume has no size limit: ``quota_mb`` is a warning shown to the agent.
    """

    volume: str
    rules: str
    connections: str
    readme: str
    quota_mb: int = 4096

    def __post_init__(self) -> None:
        if (
            HOME_VOLUME.fullmatch(self.volume) is None
            or type(self.quota_mb) is not int
            or not 64 <= self.quota_mb <= 1048576
        ) or any(
            not isinstance(text, str) or len(text.encode()) > GUIDE_BYTES
            for text in (self.rules, self.connections, self.readme)
        ):
            raise ValueError("RUNNER_HOME_INVALID")

    def wire(self) -> dict[str, object]:
        return {
            "rules": self.rules,
            "connections": self.connections,
            "readme": self.readme,
            "quota_mb": self.quota_mb,
        }


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


def entry_argument(source: str) -> str:
    """Keep trusted PID 1 code below Windows' CreateProcess command-line ceiling.

    Only platform entry code is packed, never a model command or task payload.
    The same source executes inside the same isolated Python process; no file,
    host mount, shell or network is introduced.
    """
    if len(source) <= 4096:
        return source
    packed = base64.b64encode(zlib.compress(source.encode("utf-8"), 9)).decode("ascii")
    return (
        "import base64,zlib;exec(compile(zlib.decompress(base64.b64decode('"
        + packed
        + "')),'<platform-entry>','exec'))"
    )


def create_args(
    template: RunnerTemplate,
    task: RunnerTask,
    name: str,
    entry: str,
    home: str | None = None,
    network: str | None = None,
    process_cpu_seconds: int | None = None,
) -> list[str]:
    """No host mounts/ports/keys/socket. Writable task disk is a kernel-bounded tmpfs.

    ``process_cpu_seconds`` replaces the per-turn CPU rlimit for a resident sandbox: RLIMIT_CPU
    counts a process's whole life, so its ``opencode serve`` would otherwise die after a few
    turns. Each turn stays bounded by wall time and ``--cpus``; the day by the CPU budget.

    ``home`` adds the persona's named volume at /workspace and a small tmpfs for /guide;
    everything else stays read-only or disappears with the container. ``network`` is the
    turn's internal egress network (no DNS, no route out except the gateway proxy).
    """
    template.check(task)
    if re.fullmatch(r"aw-task-[0-9a-f]{32}", name) is None:
        raise ValueError("RUNNER_NAME_INVALID")
    if home is not None and HOME_VOLUME.fullmatch(home) is None:
        raise ValueError("RUNNER_HOME_INVALID")
    if network is not None and re.fullmatch(r"aw-net-[0-9a-f]{32}", network) is None:
        raise ValueError("RUNNER_NETWORK_INVALID")
    cap = task.limits
    cpu = cap.cpu_seconds if process_cpu_seconds is None else process_cpu_seconds
    if not cap.cpu_seconds <= cpu <= 7 * 86400:
        raise ValueError("RUNNER_CPU_INVALID")
    mounts = (
        [
            "--mount",
            f"type=volume,src={home},dst=/workspace",
            "--tmpfs",
            "/guide:rw,nosuid,nodev,noexec,size=1m,nr_inodes=64,mode=0700,uid=65532,gid=65532",
        ]
        if home is not None
        else []
    )
    return [
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        "ai-agent-platform.runner=aw-04",
        "--interactive",
        "--network=" + (network or "none"),
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
        f"cpu={cpu}:{cpu}",
        "--ulimit",
        "nofile=128:128",
        "--ulimit",
        "core=0:0",
        "--tmpfs",
        f"/work:rw,nosuid,nodev,noexec,size={cap.disk_bytes},nr_inodes=2048,mode=0700,uid=65532,gid=65532",
        *mounts,
        "--workdir=/work",
        "--entrypoint=/usr/local/bin/python",
        template.image_id,
        "-I",
        "-u",
        "-c",
        entry_argument(entry),
    ]
