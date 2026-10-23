"""Offline combined checks for accepted modules, using the invoking interpreter."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "apps/core/src",
    "apps/worker/src",
    "packages/contracts/src",
    "packages/domain/src",
    "packages/application/src",
    "packages/adapters/src",
)
HELPERS = (
    "tests/persona",
    "tests/memory",
    "tests/social",
    "tests/jobs",
    "tests/probes/m0_05",
    "tests/storage",
    "tests/operations",
    "tests/access",
    "tests/llm",
    "tests/usage",
    "tests/dialogue",
    "tests/moderation",
    "tests/extraction",
    "tests/gamemaster",
    "tests/development",
    "tests/laboratory",
    "tests/m3_review",
    "tests/m3_runtime",
    "tests/local_host",
    "tests/local_backup",
    "tests/local_artifacts",
    "tests/report_lineage",
    "tests/integration_gm",
    "tests/memory_quality",
    "tests/integration_dialogue",
    "tests/retention",
    "tests/backup",
    "tests/e2e",
    "tests/evaluation",
    "tests/workplace_contracts",
    "tests/workplace_tickets",
    "tests/workplace_activity",
    "tests/workplace_files",
    "tests/workplace_skills",
    "tests/workplace_experience",
    "tests/workplace_conversation",
    "tests/workplace_sessions",
    "tests/workplace_lifecycle",
    "tests/workplace_sandbox",
    "tests/workplace_harness",
    "tests/workplace_journey",
    "tests/workplace_launcher",
)
TESTS = (
    "tests/opencode_ui",
    "tests/operations",
    "tests/access",
    "tests/jobs",
    "tests/control",
    "tests/storage",
    "tests/tooling",
    "tests/contracts",
    "tests/persona",
    "tests/memory",
    "tests/social",
    "tests/probes/m0_05",
    "tests/llm",
    "tests/usage",
    "tests/dialogue",
    "tests/moderation",
    "tests/extraction",
    "tests/gamemaster",
    "tests/development",
    "tests/laboratory",
    "tests/m3_review",
    "tests/m3_runtime",
    "tests/local_host",
    "tests/local_backup",
    "tests/local_artifacts",
    "tests/report_lineage",
    "tests/integration_gm",
    "tests/memory_quality",
    "tests/integration_dialogue",
    "tests/retention",
    "tests/backup",
    "tests/e2e",
    "tests/evaluation",
    "tests/probes/m0_06/test_configuration.py",
    "tests/workplace_contracts",
    "tests/workplace_tickets",
    "tests/workplace_files",
    "tests/workplace_skills",
    "tests/workplace_activity",
    "tests/workplace_experience",
    "tests/workplace_conversation",
    "tests/workplace_sessions",
    "tests/workplace_lifecycle",
    "tests/workplace_sandbox",
    "tests/workplace_harness",
    "tests/workplace_journey",
    "tests/workplace_launcher",
)


def main() -> int:
    environment = os.environ.copy()
    # Never import an older editable checkout through a preexisting PYTHONPATH.
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in SOURCES + HELPERS)
    commands = (
        ("-m", "ruff", "check", *SOURCES, "scripts", "tests"),
        ("-m", "mypy"),
        ("scripts/check_imports.py",),
        ("-m", "ai_agent_contracts.export", "--output", "packages/contracts/generated", "--check"),
        (
            "-m",
            "pytest",
            *TESTS,
            "--import-mode=importlib",
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
        ),
    )
    for command in commands:
        result = subprocess.run([sys.executable, *command], cwd=ROOT, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
