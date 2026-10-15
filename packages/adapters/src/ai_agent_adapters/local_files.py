"""Native owner-only filesystem permissions; POSIX modes alone do not prove Windows ACLs."""

import os
import subprocess
import sys
from pathlib import Path


class PrivatePathError(OSError):
    pass


def restrict_private(path: Path, *, directory: bool) -> None:
    if path.is_symlink() or path.is_junction():
        raise PrivatePathError("PRIVATE_PATH_UNSAFE")
    if sys.platform != "win32":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    script = r"""
$ErrorActionPreference = 'Stop'
try {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $target = $env:AI_PLATFORM_PRIVATE_TARGET
    $item = Get-Item -LiteralPath $target -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 3 }
    if ($env:AI_PLATFORM_PRIVATE_DIRECTORY -eq '1') {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $me,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($me,'FullControl','Allow')
    }
    $acl.SetAccessRuleProtection($true,$false)
    $acl.SetOwner($me)
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $target -AclObject $acl
    exit 0
} catch { exit 3 }
"""
    environment = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    environment["AI_PLATFORM_PRIVATE_TARGET"] = str(path)
    environment["AI_PLATFORM_PRIVATE_DIRECTORY"] = "1" if directory else "0"
    executable = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
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
        raise PrivatePathError("ACL_NOT_APPLIED") from None
    if result.returncode != 0:
        raise PrivatePathError("ACL_NOT_APPLIED")
