"""`python -m ai_agent_core.local_backup create|list|verify|preview-restore|restore`.

Absolute paths only. Prints fixed codes and counts, never paths, memory content or secrets.
`preview-restore` and `restore` hold the host's OS lock for the whole operation: a running
`serve` makes them refuse, and the agent is never started by a restore.
"""

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ai_agent_application.backup import RestorePreview
from ai_agent_contracts.codec import ContractError

from ai_agent_core.local_host.layout import HostError, load_config, restrict
from ai_agent_core.local_host.process import exclusive_host

from .compose import LocalBackup, compose
from .layout import BackupLayout, layout

EXIT_USAGE, EXIT_REFUSED, EXIT_FAILED = 64, 78, 1
DECISION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="ai_agent_core.local_backup")
    commands = main.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, ledger: bool) -> None:
        command.add_argument("--root", type=Path, required=True, help="absolute host directory")
        command.add_argument("--store", type=Path, required=True, help="absolute copies directory")
        if ledger:
            command.add_argument(
                "--ledger", type=Path, required=True, help="absolute existing deletion ledger file"
            )

    create = commands.add_parser("create", help="one consistent snapshot, beside a running host")
    common(create, ledger=True)
    create.add_argument("--kind", choices=("manual", "daily"), default="manual")
    common(commands.add_parser("list", help="stored copies, newest first"), ledger=False)
    verify = commands.add_parser("verify", help="checksums, sizes and integrity of one copy")
    common(verify, ledger=False)
    verify.add_argument("--backup-id", required=True)
    preview = commands.add_parser("preview-restore", help="offline: consequences of a restore")
    common(preview, ledger=True)
    preview.add_argument("--backup-id", required=True)
    restore = commands.add_parser("restore", help="offline: restore a previewed copy")
    common(restore, ledger=True)
    restore.add_argument("--backup-id", required=True)
    restore.add_argument("--decision-id", required=True, help="operator decision identifier")
    restore.add_argument("--digest", required=True, help="digest printed by preview-restore")
    return main


class _NoLedger:
    """For list/verify, which never touch deletion history: any use fails closed."""

    async def identity(self) -> tuple[str, int]:
        raise HostError("LEDGER_UNAVAILABLE")

    async def snapshot_to(self, target: Path) -> None:
        raise HostError("LEDGER_UNAVAILABLE")

    async def block(self, *args: Any) -> Any:
        raise HostError("LEDGER_UNAVAILABLE")

    async def mark_purged(self, *args: Any) -> None:
        raise HostError("LEDGER_UNAVAILABLE")

    async def entries(self, *args: Any) -> Any:
        raise HostError("LEDGER_UNAVAILABLE")


@contextmanager
def _unlocked_local(arguments: argparse.Namespace, *, offline: bool) -> Iterator[LocalBackup]:
    place: BackupLayout = layout(
        arguments.root,
        arguments.store,
        getattr(arguments, "ledger", None) or arguments.store.parent / ".no-ledger",
    )
    config = load_config(place.host)
    ledger = None if hasattr(arguments, "ledger") else _NoLedger()
    if not offline:
        local = compose(place, config, ledger=ledger)
        try:
            yield local
        finally:
            local.close()
        return
    with exclusive_host(place.host.root):  # a running serve holds it: refuse, never interleave
        local = compose(place, config, ledger=ledger)
        local.gate.held = True
        try:
            yield local
        finally:
            local.gate.held = False
            local.close()


@contextmanager
def _local(arguments: argparse.Namespace, *, offline: bool) -> Iterator[LocalBackup]:
    from ai_agent_core.local_host.recovery import no_links

    no_links(arguments.store)
    if not arguments.store.is_absolute():
        raise HostError("ABSOLUTE_PATH_REQUIRED")
    work = arguments.store / ".work"
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict(work, directory=True)
    with exclusive_host(work):
        with _unlocked_local(arguments, offline=offline) as local:
            yield local


def _print_preview(preview: RestorePreview) -> None:
    print(f"backup: {preview.backup_id} kind={preview.kind} schema={preview.schema_head}")
    print(
        f"scopes={preview.scopes} open_operations={preview.non_terminal_operations}"
        f" outbox_to_quarantine={preview.pending_outbox}"
        f" needs_migration={str(preview.needs_migration).lower()}"
    )
    for issue in preview.issues:
        print(f"issue: {issue.code} {'blocking' if issue.blocking else 'warning'}")
        if issue.code == "WORKPLACE_LEDGER_REDACTIONS":
            count = issue.detail.split(":", 1)[0]
            if count.isdecimal():
                print(f"workplace_records_to_invalidate={count}")
    print(f"restorable: {str(not preview.blocked).lower()}")
    print(f"digest: {preview.digest}")


async def _run(arguments: argparse.Namespace) -> int:
    command = arguments.command
    if command in {"list", "verify"}:
        with _local(arguments, offline=False) as local:
            if command == "list":
                for copy in await local.service.copies():
                    print(
                        f"{copy.backup_id} kind={copy.kind} created={copy.created_at.isoformat()}"
                        f" schema={copy.schema_head}"
                    )
                return 0
            issues = await local.service.verify(arguments.backup_id)
            for issue in issues:
                print(f"issue: {issue.code}")
            print(f"verified: {str(not issues).lower()}")
            return 0 if not issues else EXIT_REFUSED
    if command == "create":
        with _local(arguments, offline=False) as local:
            result = await local.service.create(arguments.kind)
            problems = await local.service.verify(result.manifest.backup_id)
            print(f"created: {result.manifest.backup_id} verified={str(not problems).lower()}")
            for warning in result.warnings:
                print(f"warning: {warning}")
            return 0 if not problems else EXIT_FAILED
    with _local(arguments, offline=True) as local:
        preview = await local.service.preview_restore(arguments.backup_id)
        if command == "preview-restore":
            _print_preview(preview)
            return 0 if not preview.blocked else EXIT_REFUSED
        if not DECISION.fullmatch(arguments.decision_id) or not DIGEST.fullmatch(arguments.digest):
            raise HostError("INVALID_OPTION")
        if preview.digest != arguments.digest:
            raise HostError("PREVIEW_CHANGED")  # the operator confirmed something else
        from ai_agent_core.local_host.resume import ResumeError, block_resume

        try:
            # Before the owner touches anything: nothing older than this restore may be resumed.
            # If the block cannot be written, the restore does not begin.
            block_resume(arguments.root)
        except ResumeError:
            raise HostError("RESUME_BLOCK_FAILED") from None
        report = await local.service.restore(preview, arguments.decision_id)
        print(f"restored: {report.backup_id} safety_copy={report.safety_backup_id}")
        print(
            f"scopes_verified={len(report.verified)} reapplied_deletions={len(report.reapplied)}"
            f" expired={report.expired} outbox_quarantined={report.quarantined_outbox}"
            f" reindexed={report.reindexed}"
        )
        print("agent state: STOPPED; replaced files are kept aside until the operator erases them")
        return 0


def _guard(work: Callable[[], Coroutine[Any, Any, int]]) -> int:
    try:
        return asyncio.run(work())
    except HostError as error:
        print(f"local-backup refused: {error.code}", file=sys.stderr)
        return EXIT_REFUSED
    except ContractError as error:
        print(f"local-backup refused: {error.error.code.value}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # the type only: a message may echo a path or content
        print(f"local-backup failed: {type(error).__name__}", file=sys.stderr)
        return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if sys.platform != "win32":
        os.umask(0o077)  # copies, work files and ledger copies are owner-only from creation
    return _guard(lambda: _run(arguments))
