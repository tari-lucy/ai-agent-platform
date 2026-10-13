"""Run local checks from any cwd; requires an already synced project environment."""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Run bounded offline checks; never install, start services or load models."""
    uv = shutil.which("uv")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if uv is None or npm is None:
        raise SystemExit("uv and npm are required; see scripts/README.md.")
    commands = [
        [
            uv,
            "run",
            "--locked",
            "--no-sync",
            "ruff",
            "check",
            "apps",
            "packages",
            "scripts",
            "tests",
        ],
        [
            uv,
            "run",
            "--locked",
            "--no-sync",
            "ruff",
            "format",
            "--check",
            "apps",
            "packages",
            "scripts",
            "tests",
        ],
        [uv, "run", "--locked", "--no-sync", "mypy"],
        [uv, "run", "--locked", "--no-sync", "python", "scripts/check_imports.py"],
        [uv, "run", "--locked", "--no-sync", "pytest"],
        [uv, "run", "--locked", "--no-sync", "ai-platform-core-check"],
        [uv, "run", "--locked", "--no-sync", "ai-platform-worker-check"],
    ]
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)
    for task in ("lint", "typecheck", "build"):
        subprocess.run([npm, "run", task], cwd=ROOT / "apps/panel", check=True)


if __name__ == "__main__":
    main()
