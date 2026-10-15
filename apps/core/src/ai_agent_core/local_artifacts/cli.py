"""`python -m ai_agent_core.local_artifacts preview|apply`: offline derived-file preparation.

Run after a database restore, with the host stopped: the host's OS lock is held for the whole
operation, so a running `serve` makes both commands refuse. Prints fixed codes and counts only.
"""

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from ai_agent_adapters.retention import SQLiteDeletionLedger
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_contracts.common import Scope

from ai_agent_core.local_host.init import test_scope, work_scope
from ai_agent_core.local_host.layout import HostConfig, HostError, load_config, paths
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.artifacts import Reports

from .restore import RestorePlan, apply_restore, plan_restore
from .roots import ArtifactsError, verified_roots

EXIT_REFUSED, EXIT_FAILED = 78, 1


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="ai_agent_core.local_artifacts")
    commands = main.add_subparsers(dest="command", required=True)
    for name in ("preview", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True, help="absolute host directory")
        command.add_argument("--ledger", type=Path, required=True, help="existing deletion ledger")
        command.add_argument(
            "--drop-reports",
            action="store_true",
            help="make every report body unavailable (catalogue records stay)",
        )
        if name == "apply":
            command.add_argument("--decision-id", required=True)
            command.add_argument("--digest", required=True, help="digest printed by preview")
    return main


def _scopes(config: HostConfig) -> list[Scope]:
    work = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    spaces = [
        *config.test_space_ids,
        *([config.settings_space_id] if config.settings_space_id else []),
    ]
    return [work, *(test_scope(work, space) for space in spaces)]


def _open_ledger(path: Path) -> SQLiteDeletionLedger:
    """Never creates a ledger: an empty or foreign file would become a new, empty history."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ArtifactsError("LEDGER_UNAVAILABLE")
        probe = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master")}
            rows = probe.execute("SELECT COUNT(*) FROM ledger_meta").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error, OSError:
        raise ArtifactsError("LEDGER_UNAVAILABLE") from None
    if not {"deletion_ledger", "ledger_meta"} <= tables or rows != 1:
        raise ArtifactsError("LEDGER_UNAVAILABLE")
    return SQLiteDeletionLedger(path)


def _print(plan: RestorePlan) -> None:
    print(f"ledger_sources={plan.ledger_sources} index_files={len(plan.indexes)}")
    for finding in plan.indexes:
        state = "recognised" if finding.recognised else "UNRECOGNISED"
        print(f"index: {state} blocked_sources={finding.blocked_sources}")
    print(f"report_files={plan.report_files} report_bytes={plan.report_bytes}")
    if plan.drop_reports:
        print("warning: every report body becomes unavailable; catalogue records stay")
    elif plan.unproven_reports:
        print(f"warning: {plan.unproven_reports} report bodies cannot be shown clean (no relation)")
    print(f"runnable: {str(not plan.blocked).lower()}")
    print(f"digest: {plan.digest}")


async def _run(arguments: argparse.Namespace) -> int:
    host = paths(arguments.root)
    with exclusive_host(host.root):
        config = load_config(host)
        roots = verified_roots(host.lab_indexes, host.reports)
        scopes = _scopes(config)
        ledger = _open_ledger(arguments.ledger)
        store = SQLiteStore(host.database, extensions=new_registry())  # never connected here
        try:
            plan = await plan_restore(roots, ledger, scopes, drop_reports=arguments.drop_reports)
            if arguments.command == "preview":
                _print(plan)
                return 0 if not plan.blocked else EXIT_REFUSED
            if plan.digest != arguments.digest:
                raise ArtifactsError("PREVIEW_CHANGED")
            reports = Reports(roots.reports, store, new_registry())
            result = await apply_restore(
                roots, ledger, scopes, plan, arguments.decision_id, reports=reports
            )
            print(
                f"derived files prepared: indexes_cleaned={result.indexes_cleaned}"
                f" sources_removed={result.sources_removed}"
                f" reports_dropped={result.reports_dropped}"
            )
            return 0
        finally:
            ledger.close()


def _guard(work: Callable[[], Coroutine[Any, Any, int]]) -> int:
    try:
        return asyncio.run(work())
    except (ArtifactsError, HostError) as error:
        print(f"local-artifacts refused: {error.code}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # the type only: a message may echo a path or text
        print(f"local-artifacts failed: {type(error).__name__}", file=sys.stderr)
        return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    return _guard(lambda: _run(arguments))
