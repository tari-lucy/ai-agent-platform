"""`python -m ai_agent_core.local_host init|serve`. Prints fixed codes, never secrets or paths."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ai_agent_adapters.configuration import ConfigurationError
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_contracts.codec import ContractError

from .init import PROFILE_LIMIT, initialise
from .layout import HostError, load_config, paths
from .serve import permissions_expired, serve

EXIT_USAGE, EXIT_CONFIG, EXIT_FAILED = 64, 78, 1


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="ai_agent_core.local_host")
    commands = main.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="prepare a new, empty host directory once")
    init.add_argument("--root", type=Path, required=True, help="absolute path of a new directory")
    init.add_argument("--profile-file", type=Path, help="persona Markdown; default: synthetic")
    init.add_argument("--workspace-id", default="local")
    init.add_argument("--persona-id", default="persona")
    init.add_argument("--port", type=int, default=8765)
    init.add_argument("--test-scopes", type=int, default=4, help="empty TEST scopes, 1..7")
    init.add_argument("--permission-days", type=int, default=30, help="lifetime, 1..90 days")
    init.add_argument("--migrations", type=Path, help="override the repository migrations")
    run = commands.add_parser("serve", help="serve a prepared host STOPPED on 127.0.0.1")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument(
        "--provider",
        choices=("mock", "live"),
        required=True,
        help="mock is the explicit technical mode; live is refused until accepted",
    )
    run.add_argument("--port", type=int, help="override the prepared port")
    run.add_argument(
        "--panel-dir", type=Path, help="absolute path of a built apps/panel/dist to serve"
    )
    return main


def read_profile(path: Path | None) -> str:
    if path is None:
        return REFERENCE_PROFILE
    try:
        with path.open("rb") as handle:
            raw = handle.read(PROFILE_LIMIT + 1)
        return raw.decode("utf-8")
    except OSError, UnicodeDecodeError:
        raise HostError("PROFILE_UNREADABLE") from None


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if sys.platform != "win32":
        os.umask(0o077)  # databases, WAL files, indexes and reports are owner-only from creation
    try:
        if arguments.command == "init":
            config = asyncio.run(
                initialise(
                    arguments.root,
                    profile_markdown=read_profile(arguments.profile_file),
                    workspace_id=arguments.workspace_id,
                    persona_id=arguments.persona_id,
                    port=arguments.port,
                    test_scopes=arguments.test_scopes,
                    permission_days=arguments.permission_days,
                    migrations=arguments.migrations,
                )
            )
            where = paths(arguments.root)
            pool = len(config.test_space_ids)
            print(f"init complete: persona={config.persona_id} test_scopes={pool}")
            print(f"operator password file (owner-only): {where.password}")
            return 0
        config = load_config(paths(arguments.root))
        if permissions_expired(config):
            print("NOTICE: prepared permissions expired; start stays closed", file=sys.stderr)
        print("serving STOPPED on loopback; start is an explicit operator command", flush=True)
        serve(
            arguments.root,
            provider=arguments.provider,
            port=arguments.port,
            panel_dir=arguments.panel_dir,
        )
        return 0
    except HostError as error:
        print(f"local-host refused: {error.code}", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIG
    except ContractError as error:
        print(f"local-host failed: {error.error.code}", file=sys.stderr)
        return EXIT_FAILED
