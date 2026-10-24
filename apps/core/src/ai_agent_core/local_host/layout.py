"""Host directory layout, nonsecret configuration and local secret files."""

import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from ai_agent_adapters.local_files import PrivatePathError, restrict_private
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_contracts.agent_workplace import WRITABLE_AREAS, WorkplaceArea
from ai_agent_contracts.common import Scope
from ai_agent_contracts.panel import ApiLimitChange
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"
MAX_TEST_SCOPES = 7  # the runtime accepts at most 8 scopes including the work scope
MAX_CONFIG_BYTES = 64 * 1024
GM_PENDING = "GM-CHANGE-PENDING"
PERSONA_PENDING = "PERSONA-ADD-PENDING"


class HostError(Exception):
    """Fixed code only; never a path, value or chained exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GmConfig(BaseModel):
    """Bounded internal game master for the private pilot. Off unless explicitly enabled offline.

    The line is the operator's own story direction; the game master never invents one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = Field(default=False, strict=True)
    daily_offers: int = Field(default=2, strict=True, ge=1, le=8)
    daily_calls: int = Field(default=4, strict=True, ge=2, le=16)
    interval_minutes: int = Field(default=60, strict=True, ge=15, le=1440)
    dialogue_floor: int = Field(default=10, strict=True, ge=1, le=1000)
    line_direction: str | None = Field(default=None, min_length=1, max_length=1000)
    line_boundaries: str = Field(default="", max_length=1000)


class ActivityConfig(BaseModel):
    """Workplace activity (AW-05). Off unless explicitly enabled offline; same daily limit."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = Field(default=False, strict=True)
    daily_calls: int = Field(default=20, strict=True, ge=1, le=1000)
    stream_tools: bool = Field(default=True, strict=True)
    cooldown_minutes: int = Field(default=10, strict=True, ge=1, le=1440)
    own_goals: bool = Field(default=False, strict=True)
    own_goal_daily: int = Field(default=2, strict=True, ge=0, le=24)
    max_open_tasks: int = Field(default=20, strict=True, ge=1, le=200)


class LiveConfig(BaseModel):
    """Explicit private-dialogue pilot. Contains no credentials or endpoint values."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    model_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    attempt_limit: int = Field(default=10, strict=True, ge=1, le=1_000_000)
    attempt_period: Literal["lifetime", "utc_day"] = "lifetime"
    budget_version: int = Field(default=1, strict=True, ge=1)
    budget_receipt: ApiLimitChange | None = None
    memory_processing: bool = Field(default=False, strict=True)
    memory_daily_calls: int = Field(default=8, strict=True, ge=3, le=32)
    max_output_tokens: int = Field(default=512, strict=True, ge=32, le=1024)
    max_prompt_chars: int = Field(default=24000, strict=True, ge=1000, le=40000)
    timeout_seconds: float = Field(default=60.0, gt=0, le=120)
    gm: GmConfig = Field(default_factory=GmConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)

    @model_validator(mode="after")
    def memory_budget(self) -> Self:
        if self.budget_receipt is not None and (
            self.budget_receipt.budget_version != self.budget_version
            or self.budget_receipt.attempt_limit != self.attempt_limit
            or self.budget_receipt.period != self.attempt_period
        ):
            raise ValueError("BUDGET_RECEIPT_MISMATCH")
        if self.memory_processing and (
            self.attempt_period != "utc_day" or self.memory_daily_calls >= self.attempt_limit
        ):
            raise ValueError("DAILY_MEMORY_BUDGET_REQUIRED")
        reserved = self.memory_daily_calls if self.memory_processing else 0
        if self.gm.enabled and (
            self.attempt_period != "utc_day"
            or reserved + self.gm.daily_calls + self.gm.dialogue_floor > self.attempt_limit
        ):
            raise ValueError("DAILY_GM_BUDGET_REQUIRED")
        gm = self.gm.daily_calls if self.gm.enabled else 0
        if self.activity.enabled and (
            self.attempt_period != "utc_day"
            or reserved + gm + self.activity.daily_calls + self.gm.dialogue_floor
            > self.attempt_limit
        ):
            raise ValueError("DAILY_ACTIVITY_BUDGET_REQUIRED")
        return self


def live_roles(live: LiveConfig | None) -> frozenset[str]:
    """The single rule of which roles may be bound to the live connection."""
    from ai_agent_core.m3_composition.state import MEMORY_ROLES, ROLES

    if live is None:
        return frozenset()
    roles = {"character"}
    if live.memory_processing:
        roles |= MEMORY_ROLES
    if live.gm.enabled:
        roles |= set(ROLES["gm"])
    if live.activity.enabled:
        roles |= set(ROLES["activity"])
    return frozenset(roles)


def assignments_fit(
    config: HostConfig, bindings: dict[tuple[str, str, str], dict[str, Any]]
) -> bool:
    """Backup/restore rule: every role assignment matches what this host config admits."""
    admitted = live_roles(config.live)
    work = scope_key(
        Scope(
            workspace_id=config.workspace_id,
            persona_id=config.persona_id,
            memory_space_id=config.work_space_id,
            memory_mode="work",
        )
    )
    for (partition, kind, _), row in bindings.items():
        if kind != "LLMAssignment":
            continue
        if row.get("connection_id") not in ("conn-live", "conn-mock"):
            return False
        live = row.get("connection_id") == "conn-live"
        if partition != work:
            if live:
                return False
            continue
        if live != (row.get("role_id") in admitted):
            return False
        if live and (config.live is None or row.get("model_id") != config.live.model_id):
            return False
    return True


class RecoveryConfig(BaseModel):
    """Local paths and monotonic ledger binding; never credentials or source text."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    store_dir: str = Field(min_length=1, max_length=4096)
    ledger_path: str = Field(min_length=1, max_length=4096)
    ledger_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    ledger_floor: int = Field(default=0, strict=True, ge=0)
    interval_seconds: int = Field(default=300, strict=True, ge=60, le=3600)
    keep_daily: int = Field(default=7, strict=True, ge=1, le=30)
    backup_quota_bytes: int = Field(default=1073741824, strict=True, ge=1048576)


class PermissionCeiling(BaseModel):
    """Host-configured maximum the operator may grant for one workplace capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    level: Literal["prepare_only", "operator_decision", "automatic"]
    max_days: int = Field(ge=1, le=365)


class WorkplaceFilesConfig(BaseModel):
    """Private managed-file store (AW-02/AW-03) and its quotas; never inside the database."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    blob_dir: str = Field(min_length=1, max_length=4096)
    max_file_bytes: int = Field(default=1048576, strict=True, ge=1024, le=67108864)
    max_scope_bytes: int = Field(default=268435456, strict=True, ge=1048576, le=17179869184)
    max_files: int = Field(default=2000, strict=True, ge=10, le=100000)
    max_versions: int = Field(default=20000, strict=True, ge=10, le=1000000)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.max_file_bytes > self.max_scope_bytes or self.max_files > self.max_versions:
            raise ValueError("WORKPLACE_FILE_LIMITS")
        return self


class WorkSessionsConfig(BaseModel):
    """Work conversations executed by a pinned OpenCode image (R5 launcher); data, no persona.

    The image is referenced by its immutable ID and verified offline; every limit here is a
    ceiling of the trusted runner template, never chosen by instruction text. Grants no right.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability_id: str = Field(
        default="workplace.opencode-session", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
    )
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    areas: tuple[WorkplaceArea, ...] = Field(min_length=1, max_length=8)
    output_area: WorkplaceArea
    memory_mb: int = Field(default=1024, strict=True, ge=256, le=8192)
    pids: int = Field(default=128, strict=True, ge=16, le=1024)
    cpu_seconds: int = Field(default=60, strict=True, ge=5, le=3600)
    wall_seconds: int = Field(default=120, strict=True, ge=10, le=3600)
    disk_mb: int = Field(default=32, strict=True, ge=8, le=1024)
    output_bytes: int = Field(default=65536, strict=True, ge=1024, le=1048576)
    max_input_bytes: int = Field(default=262144, strict=True, ge=1024, le=16777216)
    max_files: int = Field(default=16, strict=True, ge=1, le=200)
    max_model_calls: int = Field(default=6, strict=True, ge=1, le=16)
    max_prompt_chars: int = Field(default=100000, strict=True, ge=1000, le=100000)
    max_model_tokens: int = Field(default=1024, strict=True, ge=64, le=65536)
    max_workspace_calls: int = Field(default=16, strict=True, ge=0, le=64)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if (
            self.output_area not in self.areas
            or self.output_area not in WRITABLE_AREAS
            or len(set(self.areas)) != len(self.areas)
            or self.cpu_seconds > self.wall_seconds
        ):
            raise ValueError("WORK_SESSIONS_LIMITS")
        return self


class InactivePersona(BaseModel):
    """Prepared work partition, never an additional executor or model assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    persona_id: str = Field(pattern=IDENTIFIER)
    work_space_id: str = Field(pattern=IDENTIFIER)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class HostConfig(BaseModel):
    """Nonsecret result of `init`. Its presence (without the marker) means a finished init."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    workspace_id: str = Field(pattern=IDENTIFIER)
    persona_id: str = Field(pattern=IDENTIFIER)
    work_space_id: str = Field(pattern=IDENTIFIER)
    additional_personas: tuple[InactivePersona, ...] = Field(
        default=(), max_length=7, exclude_if=lambda value: not value
    )
    test_space_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_TEST_SCOPES)
    port: int = Field(ge=1024, le=65535)
    permission_expires_at: str
    operator_controls: bool = False
    live: LiveConfig | None = None
    recovery: RecoveryConfig | None = None
    settings_space_id: str | None = Field(default=None, pattern=IDENTIFIER)
    # The panel login is a stable social interlocutor only after its rights were provisioned.
    private_social: bool = False
    # Operator tickets (AW-01) only after `enable-tickets` provisioned the TICKETS rows.
    workplace_tickets: bool = False
    # Structured permission decisions (AW-01); None keeps them unavailable. Only capabilities
    # listed here can be granted, never above their level or duration.
    workplace_permissions: dict[str, PermissionCeiling] | None = None
    # Managed files, skills and knowledge (AW-03) only after `enable-workplace-files`; they
    # need the deletion ledger (recovery) for the release guard.
    workplace_files: WorkplaceFilesConfig | None = None
    # Work conversations through real OpenCode only after `enable-work-sessions`; off otherwise.
    work_sessions: WorkSessionsConfig | None = None

    @model_validator(mode="after")
    def scope_layout(self) -> Self:
        if self.live is not None and self.live.memory_processing and not self.operator_controls:
            raise ValueError("MEMORY_CONTROLS_REQUIRED")
        if self.operator_controls != (self.settings_space_id is not None):
            raise ValueError("OPERATOR_SETTINGS_SCOPE")
        if self.workplace_permissions is not None and (
            not self.workplace_tickets
            or not self.operator_controls
            or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key)
                for key in self.workplace_permissions
            )
        ):
            raise ValueError("WORKPLACE_PERMISSIONS")
        if self.workplace_files is not None and (
            self.recovery is None or not self.operator_controls
        ):
            raise ValueError("WORKPLACE_FILES")
        if self.work_sessions is not None and (
            self.workplace_files is None or not self.workplace_tickets
        ):
            raise ValueError("WORK_SESSIONS")
        spaces = [self.work_space_id, *self.test_space_ids]
        if self.settings_space_id is not None:
            spaces.append(self.settings_space_id)
        spaces.extend(p.work_space_id for p in self.additional_personas)
        people = [self.persona_id, *(p.persona_id for p in self.additional_personas)]
        if (
            len(spaces) > MAX_TEST_SCOPES + 1
            or len(set(spaces)) != len(spaces)
            or len(set(people)) != len(people)
        ):
            raise ValueError("SCOPE_LAYOUT")
        return self


def host_scopes(config: HostConfig) -> tuple[Scope, ...]:
    """All operator partitions (at most eight); only the first is the runtime's work scope."""
    work = Scope(
        workspace_id=config.workspace_id,
        persona_id=config.persona_id,
        memory_space_id=config.work_space_id,
        memory_mode="work",
    )
    tests = (
        *config.test_space_ids,
        *((config.settings_space_id,) if config.settings_space_id else ()),
    )
    return (
        work,
        *(
            work.model_copy(update={"memory_space_id": space, "memory_mode": "test"})
            for space in tests
        ),
        *(
            work.model_copy(update={"persona_id": p.persona_id, "memory_space_id": p.work_space_id})
            for p in config.additional_personas
        ),
    )


@dataclass(frozen=True)
class HostPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "host.json"

    @property
    def unfinished(self) -> Path:
        return self.root / "INIT-UNFINISHED"

    @property
    def database(self) -> Path:
        return self.root / "data" / "platform.sqlite"

    @property
    def dialogue_index(self) -> Path:
        return self.root / "data" / "dialogue-index.sqlite"

    @property
    def lab_indexes(self) -> Path:
        return self.root / "data" / "lab-indexes"

    @property
    def reports(self) -> Path:
        return self.root / "data" / "reports"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def password(self) -> Path:
        return self.secrets / "operator-password.txt"

    @property
    def password_hash(self) -> Path:
        return self.secrets / "operator-password.hash"


def paths(root: Path) -> HostPaths:
    if not root.is_absolute():
        raise HostError("ABSOLUTE_ROOT_REQUIRED")  # no cwd-dependent location
    return HostPaths(root)


def restrict(path: Path, *, directory: bool) -> None:
    """Replace the full DACL; explicit broad ACEs from mkdir(0700) must not survive."""
    try:
        restrict_private(path, directory=directory)
    except PrivatePathError:
        raise HostError("ACL_NOT_APPLIED") from None


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    restrict(path, directory=True)


def write_secret(path: Path, text: str) -> None:
    """Create exclusively (never overwrite) with owner-only mode from the first byte."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    restrict(path, directory=False)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def windows_private(path: Path, *, parents: int = 2) -> bool:
    """Check file, secret directory and host-root DACL by SID without printing identities.

    The existing init uses icacls to protect each new directory/file. Serve must
    refuse later broadening as well. PowerShell returns an exit code only; the
    path is passed through a dedicated environment variable, never script text.
    """
    script = r"""
$ErrorActionPreference = 'Stop'
try {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $file = $env:AI_PLATFORM_ACL_TARGET
    $targets = @($file)
    for ($i = 0; $i -lt [int]$env:AI_PLATFORM_ACL_PARENTS; $i++) {
        $targets += Split-Path -LiteralPath $targets[-1]
    }
    foreach ($target in $targets) {
        $item = Get-Item -LiteralPath $target -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { exit 3 }
        $acl = Get-Acl -LiteralPath $target
        if (-not $acl.AreAccessRulesProtected) { exit 3 }
        if ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $me.Value) {
            exit 3
        }
        $own = $false
        foreach ($rule in $acl.GetAccessRules($true, $true,
                    [System.Security.Principal.SecurityIdentifier])) {
            $allow = [System.Security.AccessControl.AccessControlType]::Allow
            if ($rule.AccessControlType -eq $allow) {
                if ($rule.IdentityReference.Value -ne $me.Value) { exit 3 }
                $own = $true
            }
        }
        if (-not $own) { exit 3 }
    }
    exit 0
} catch { exit 3 }
"""
    environment = os.environ.copy()
    # A parent pwsh 7 PSModulePath can break Windows PowerShell 5.1 module loading.
    environment = {k: v for k, v in environment.items() if k.upper() != "PSMODULEPATH"}
    environment["AI_PLATFORM_ACL_TARGET"] = str(path)
    environment["AI_PLATFORM_ACL_PARENTS"] = str(parents)
    executable = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "System32"
        / "WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def read_secret(path: Path) -> str:
    """Refuse a secret readable by other local users; never repair permissions on serve."""
    try:
        if sys.platform == "win32":
            if not windows_private(path):
                raise HostError("SECRET_PERMISSIONS")
        else:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
                or path.parent.stat().st_mode & 0o077
            ):
                raise HostError("SECRET_PERMISSIONS")
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise HostError("SECRET_UNREADABLE") from None


def load_config(
    host: HostPaths,
    *,
    allow_memory_pending: bool = False,
    allow_gm_pending: bool = False,
    allow_persona_pending: bool = False,
) -> HostConfig:
    if not allow_persona_pending and (
        (host.root / PERSONA_PENDING).exists() or (host.root / PERSONA_PENDING).is_symlink()
    ):
        raise HostError("PERSONA_ADD_INCOMPLETE")
    if (host.root / "RECOVERY-ENABLE-PENDING").exists():
        raise HostError("RECOVERY_ENABLE_INCOMPLETE")
    if not allow_memory_pending and (host.root / "MEMORY-ENABLE-PENDING").exists():
        raise HostError("MEMORY_ENABLE_INCOMPLETE")
    if not allow_gm_pending and (host.root / GM_PENDING).exists():
        raise HostError("GM_CHANGE_INCOMPLETE")
    if host.unfinished.exists() or not host.config.is_file():
        raise HostError("INIT_INCOMPLETE")
    try:
        with host.config.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise HostError("CONFIG_INVALID")
        return HostConfig.model_validate_json(raw)
    except OSError, ValueError, ValidationError:
        raise HostError("CONFIG_INVALID") from None


def save_config(host: HostPaths, config: HostConfig) -> None:
    """Atomic local metadata update; caller must hold the host process lock."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=host.root,
            prefix=".host-config-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(config.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(host.config)
    except OSError:
        raise HostError("CONFIG_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
