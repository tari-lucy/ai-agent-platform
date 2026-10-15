"""`python -m ai_agent_core.local_host init|serve`. Prints fixed codes, never secrets or paths."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ai_agent_adapters.configuration import ConfigurationError
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_contracts.codec import ContractError
from pydantic import ValidationError

from ai_agent_core.local_artifacts import ArtifactsError

from .budget import configure_budget
from .finish_restore import finish_restore
from .init import PROFILE_LIMIT, initialise
from .layout import HostError, LiveConfig, load_config, paths
from .memory import enable_memory
from .recovery import enable_recovery
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
    init.add_argument(
        "--operator-controls",
        action="store_true",
        help="explicit owner plans/receipts and a separate TEST settings profile",
    )
    init.add_argument("--permission-days", type=int, default=30, help="lifetime, 1..90 days")
    init.add_argument("--live-model", help="explicit exact model ID for private API dialogue")
    init.add_argument("--live-attempt-limit", type=int, default=10, help="durable pilot call cap")
    init.add_argument("--live-max-output-tokens", type=int, default=512)
    init.add_argument("--migrations", type=Path, help="override the repository migrations")
    run = commands.add_parser("serve", help="serve a prepared host STOPPED on 127.0.0.1")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument(
        "--provider",
        choices=("mock", "live"),
        required=True,
        help="explicit technical mock or a previously prepared private live pilot",
    )
    run.add_argument("--env-file", type=Path, help="absolute local credential binding; live only")
    run.add_argument("--port", type=int, help="override the prepared port")
    run.add_argument(
        "--panel-dir", type=Path, help="absolute path of a built apps/panel/dist to serve"
    )
    run.add_argument(
        "--open-browser", action="store_true", help="open panel once after successful startup"
    )
    budget = commands.add_parser(
        "configure-api-budget", help="explicit offline cap update; stop the server first"
    )
    budget.add_argument("--root", type=Path, required=True)
    budget.add_argument("--attempt-limit", type=int, required=True)
    budget.add_argument("--period", choices=("lifetime", "utc_day"), required=True)
    memory = commands.add_parser(
        "enable-memory", help="explicit offline automatic memory admission"
    )
    memory.add_argument("--root", type=Path, required=True)
    recovery = commands.add_parser(
        "enable-recovery", help="offline independent ledger and daily copies"
    )
    recovery.add_argument("--root", type=Path, required=True)
    recovery.add_argument(
        "--store", type=Path, required=True, help="new absolute private copies directory"
    )
    recovery.add_argument(
        "--ledger", type=Path, required=True, help="ledger in a new absolute private directory"
    )
    finish = commands.add_parser("finish-restore", help="offline preview/apply of derived cleanup")
    finish.add_argument("--root", type=Path, required=True)
    finish.add_argument("--drop-reports", action="store_true")
    finish.add_argument("--digest")
    finish.add_argument("--decision-id")
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
        if arguments.command == "finish-restore":
            asyncio.run(
                finish_restore(
                    arguments.root,
                    drop_reports=arguments.drop_reports,
                    digest=arguments.digest,
                    decision_id=arguments.decision_id,
                )
            )
            return 0
        if arguments.command == "enable-recovery":
            asyncio.run(
                enable_recovery(
                    arguments.root, store_dir=arguments.store, ledger_path=arguments.ledger
                )
            )
            print("recovery prepared; daily copies enabled on serve; agent stays STOPPED")
            return 0
        if arguments.command == "enable-memory":
            asyncio.run(enable_memory(arguments.root))
            print("automatic daily memory enabled; usage retained; agent stays STOPPED")
            return 0
        if arguments.command == "configure-api-budget":
            configure_budget(arguments.root, limit=arguments.attempt_limit, period=arguments.period)
            print("API budget configured; usage history retained; restart remains STOPPED")
            return 0
        if arguments.command == "init":
            try:
                live = (
                    None
                    if arguments.live_model is None
                    else LiveConfig(
                        model_id=arguments.live_model,
                        attempt_limit=arguments.live_attempt_limit,
                        max_output_tokens=arguments.live_max_output_tokens,
                    )
                )
            except ValidationError:
                raise HostError("INVALID_OPTION") from None
            if live is None and (
                arguments.live_attempt_limit != 10 or arguments.live_max_output_tokens != 512
            ):
                raise HostError("LIVE_MODEL_REQUIRED")
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
                    operator_controls=arguments.operator_controls,
                    live=live,
                )
            )
            pool = len(config.test_space_ids)
            print(f"init complete: persona={config.persona_id} test_scopes={pool}")
            print("operator password file (owner-only): <root>/secrets/operator-password.txt")
            return 0
        config = load_config(paths(arguments.root))
        if permissions_expired(config):
            print("NOTICE: prepared permissions expired; start stays closed", file=sys.stderr)
        print("checking prepared local host; agent start requires an explicit command", flush=True)
        serve(
            arguments.root,
            provider=arguments.provider,
            port=arguments.port,
            panel_dir=arguments.panel_dir,
            env_file=arguments.env_file,
            open_browser=arguments.open_browser,
        )
        return 0
    except (HostError, ArtifactsError) as error:
        print(f"local-host refused: {error.code}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError:
        print("local-host refused: LOCAL_IO_FAILED", file=sys.stderr)
        return EXIT_CONFIG
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIG
    except ContractError as error:
        print(f"local-host failed: {error.error.code}", file=sys.stderr)
        return EXIT_FAILED
