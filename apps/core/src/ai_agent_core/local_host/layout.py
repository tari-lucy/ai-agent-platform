"""Host directory layout, nonsecret configuration and local secret files."""

import json
import os
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"
MAX_TEST_SCOPES = 7  # the runtime accepts at most 8 scopes including the work scope
MAX_CONFIG_BYTES = 64 * 1024


class HostError(Exception):
    """Fixed code only; never a path, value or chained exception text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HostConfig(BaseModel):
    """Nonsecret result of `init`. Its presence (without the marker) means a finished init."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    workspace_id: str = Field(pattern=IDENTIFIER)
    persona_id: str = Field(pattern=IDENTIFIER)
    work_space_id: str = Field(pattern=IDENTIFIER)
    test_space_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_TEST_SCOPES)
    port: int = Field(ge=1024, le=65535)
    permission_expires_at: str


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
    """Owner-only access by the OS's own means. Fails closed when it cannot be applied."""
    if sys.platform == "win32":
        # Fresh init paths only; read_secret independently verifies SID-based DACLs.
        user = os.environ.get("USERNAME", "")
        if not user:
            raise HostError("ACL_NOT_APPLIED")
        grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
        done = subprocess.run(  # noqa: S603
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if done.returncode != 0:
            raise HostError("ACL_NOT_APPLIED")
        owned = subprocess.run(
            ["icacls", str(path), "/setowner", user],
            capture_output=True,
            check=False,
        )
        if owned.returncode != 0:
            raise HostError("ACL_NOT_APPLIED")
        return
    os.chmod(path, 0o700 if directory else 0o600)


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


def windows_private(path: Path) -> bool:
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
    foreach ($target in @($file, (Split-Path -LiteralPath $file),
                         (Split-Path -LiteralPath (Split-Path -LiteralPath $file)))) {
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


def load_config(host: HostPaths) -> HostConfig:
    if host.unfinished.exists() or not host.config.is_file():
        raise HostError("INIT_INCOMPLETE")
    try:
        with host.config.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise HostError("CONFIG_INVALID")
        return HostConfig.model_validate(json.loads(raw))
    except OSError, ValueError, ValidationError:
        raise HostError("CONFIG_INVALID") from None
