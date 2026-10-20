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
from .gm import configure_gm
from .init import PROFILE_LIMIT, initialise
from .layout import HostError, LiveConfig, load_config, paths
from .llm_panel import prepare_llm_panel
from .memory import enable_memory
from .recovery import enable_recovery
from .serve import permissions_expired, serve
from .social import enable_social

EXIT_USAGE, EXIT_CONFIG, EXIT_FAILED = 64, 78, 1


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="ai_agent_core.local_host")
    commands = main.add_subparsers(dest="command", required=True)
    llm_panel = commands.add_parser(
        "prepare-llm-panel", help="offline operator read access to connections and assignments"
    )
    llm_panel.add_argument("--root", type=Path, required=True)
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
    social = commands.add_parser(
        "enable-social",
        help="explicit offline rights for the private interlocutor and confirmed promises",
    )
    social.add_argument("--root", type=Path, required=True)
    gm = commands.add_parser(
        "configure-gm", help="explicit offline internal game master switch and limits"
    )
    gm.add_argument("--root", type=Path, required=True)
    switch = gm.add_mutually_exclusive_group()
    switch.add_argument("--enable", action="store_true")
    switch.add_argument("--disable", action="store_true")
    gm.add_argument("--daily-offers", type=int)
    gm.add_argument("--daily-calls", type=int)
    gm.add_argument("--interval-minutes", type=int)
    gm.add_argument("--dialogue-floor", type=int)
    gm.add_argument("--line-direction-file", type=Path, help="UTF-8 text of the story line")
    gm.add_argument("--line-boundaries-file", type=Path, help="UTF-8 text of its boundaries")
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


def read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(4001)
        if len(raw) > 4000:
            raise HostError("TEXT_TOO_LONG")
        return raw.decode("utf-8").strip()
    except OSError, UnicodeDecodeError:
        raise HostError("TEXT_UNREADABLE") from None


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
        if arguments.command == "prepare-llm-panel":
            asyncio.run(prepare_llm_panel(arguments.root))
            print("LLM metadata prepared for the panel; agent stays STOPPED")
            return 0
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
        if arguments.command == "configure-gm":
            changes: dict[str, object] = {}
            if arguments.enable or arguments.disable:
                changes["enabled"] = bool(arguments.enable)
            for name in ("daily_offers", "daily_calls", "interval_minutes", "dialogue_floor"):
                if getattr(arguments, name) is not None:
                    changes[name] = getattr(arguments, name)
            if arguments.line_direction_file is not None:
                changes["line_direction"] = read_text(arguments.line_direction_file)
            if arguments.line_boundaries_file is not None:
                changes["line_boundaries"] = read_text(arguments.line_boundaries_file)
            settings = asyncio.run(configure_gm(arguments.root, **changes))
            print(
                f"game master enabled={str(settings.enabled).lower()}"
                f" daily_offers={settings.daily_offers} daily_calls={settings.daily_calls}"
                f" interval_minutes={settings.interval_minutes}"
                f" line={'set' if settings.line_direction else 'none'}; agent stays STOPPED"
            )
            return 0
        if arguments.command == "enable-memory":
            asyncio.run(enable_memory(arguments.root))
            print("automatic daily memory enabled; usage retained; agent stays STOPPED")
            return 0
        if arguments.command == "enable-social":
            asyncio.run(enable_social(arguments.root))
            print("private interlocutor and confirmed promises enabled; agent stays STOPPED")
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
