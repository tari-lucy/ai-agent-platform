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

from .activity import configure_activity
from .budget import configure_budget, configure_prompt_limit
from .finish_restore import finish_restore
from .gm import configure_gm
from .http_transport import configure_http_transport
from .init import PROFILE_LIMIT, initialise
from .layout import HostError, LiveConfig, load_config, paths
from .llm_panel import prepare_llm_panel
from .memory import enable_memory
from .personas import add_persona
from .recovery import enable_recovery
from .serve import permissions_expired, serve
from .social import enable_social
from .tickets import enable_tickets
from .work_sessions import check as check_work_sessions
from .work_sessions import enable_work_sessions
from .workplace_files import enable_workplace_files

EXIT_USAGE, EXIT_CONFIG, EXIT_FAILED = 64, 78, 1
SESSION_LIMITS = (
    "wake_max_per_hour",
    "resident_idle_seconds",
    "egress_max_mb",
    "egress_connections",
    "egress_daily_mb",
    "cpu_daily_minutes",
    "home_quota_mb",
    "memory_mb",
    "pids",
    "cpu_seconds",
    "wall_seconds",
    "disk_mb",
    "max_model_calls",
    "max_model_tokens",
    "max_workspace_calls",
)

HELPER_LIMITS = (
    "helper_max_calls",
    "helper_wall_seconds",
    "helper_cpu_seconds",
    "helper_memory_mb",
    "helper_pids",
    "helper_disk_mb",
    "helper_output_bytes",
    "helper_max_files",
    "helper_max_input_bytes",
)


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="ai_agent_core.local_host")
    commands = main.add_subparsers(dest="command", required=True)
    http = commands.add_parser(
        "configure-http", help="offline shared HTTP pool keep-alive; no API calls"
    )
    http.add_argument("--root", type=Path, required=True)
    http.add_argument(
        "--keepalive-seconds", type=int, required=True, help="idle expiry, 0..3600 seconds"
    )
    persona = commands.add_parser("add-persona", help="offline independent inactive work profile")
    persona.add_argument("--root", type=Path, required=True)
    persona.add_argument("--persona-id", required=True)
    persona.add_argument("--profile-file", type=Path, help="Markdown; default: neutral reference")
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
        "--opencode-ui-dir",
        type=Path,
        help="prepared upstream adapter build for /opencode; does not grant rights or start agents",
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
    prompt = commands.add_parser(
        "configure-prompt-limit",
        help="offline live limits (prompt chars, output tokens, timeout); stop the server first",
    )
    prompt.add_argument("--root", type=Path, required=True)
    prompt.add_argument("--max-prompt-chars", type=int)
    prompt.add_argument("--max-output-tokens", type=int)
    prompt.add_argument("--timeout-seconds", type=float)
    memory = commands.add_parser(
        "enable-memory", help="explicit offline automatic memory admission"
    )
    memory.add_argument("--root", type=Path, required=True)
    social = commands.add_parser(
        "enable-social",
        help="explicit offline rights for the private interlocutor and confirmed promises",
    )
    social.add_argument("--root", type=Path, required=True)
    tickets = commands.add_parser(
        "enable-tickets", help="explicit offline rights for operator tickets in the panel"
    )
    tickets.add_argument("--root", type=Path, required=True)
    files = commands.add_parser(
        "enable-workplace-files",
        help="explicit offline private file store, skills and knowledge; grants no agent right",
    )
    files.add_argument("--root", type=Path, required=True)
    files.add_argument("--blob-dir", type=Path, help="new private directory; default in --root")
    files.add_argument(
        "--ceiling-days",
        type=int,
        help="operator may grant these capabilities up to N days; 0: until revoked (needs tickets)",
    )
    files.add_argument("--max-file-bytes", type=int)
    files.add_argument("--max-scope-bytes", type=int)
    sessions = commands.add_parser(
        "enable-work-sessions",
        help="explicit offline work conversations through a pinned OpenCode image; no right",
    )
    sessions.add_argument("--root", type=Path, required=True)
    sessions.add_argument("--image-id", required=True, help="immutable sha256 image ID")
    sessions.add_argument("--area", action="append", required=True, dest="areas")
    sessions.add_argument("--output-area", required=True)
    sessions.add_argument(
        "--prewarm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="prepare one empty one-use OpenCode sandbox after START",
    )
    sessions.add_argument("--prewarm-idle-seconds", type=int, default=None)
    sessions.add_argument(
        "--monitor-interval-seconds",
        type=float,
        default=None,
        help="background full-proof spacing, 0.1..1.0 s; STOP polling stays 0.1 s",
    )
    sessions.add_argument(
        "--ceiling-days",
        type=int,
        help="let the operator grant the capability up to N days; 0: until revoked",
    )
    for name in SESSION_LIMITS:
        sessions.add_argument("--" + name.replace("_", "-"), type=int)
    sessions.add_argument(
        "--helper",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable the managed Python script container beside the session (no network)",
    )
    sessions.add_argument(
        "--helper-capability-id",
        default=None,
        help="scripts capability id; default workplace.scripts",
    )
    for name in HELPER_LIMITS:
        sessions.add_argument("--" + name.replace("_", "-"), type=int)
    sessions.add_argument(
        "--home",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="persistent per-persona /workspace volume with a shell inside the container",
    )
    sessions.add_argument(
        "--resident",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="keep the persona's sandbox between turns (background work survives until idle/STOP)",
    )
    sessions.add_argument(
        "--self-work",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="let the agent set its own wake-ups (fire only while ACTIVE and autonomous)",
    )
    sessions.add_argument(
        "--quiet-hours",
        default=None,
        metavar="START-END",
        help="local host hours with no wake-ups, e.g. 23-8; empty string clears",
    )
    sessions.add_argument(
        "--internet",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="internet through the per-turn egress gateway (needs the operator's permission)",
    )
    sessions.add_argument(
        "--egress-deny",
        action="append",
        default=None,
        metavar="CIDR",
        help="extra network the gateway refuses (repeat); private/LAN ranges are always refused",
    )
    check = commands.add_parser(
        "check-work-sessions", help="readiness of Docker and the configured OpenCode image"
    )
    check.add_argument("--root", type=Path, required=True)
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
    activity = commands.add_parser(
        "configure-activity", help="explicit offline workplace activity switch and limits"
    )
    activity.add_argument("--root", type=Path, required=True)
    toggle = activity.add_mutually_exclusive_group()
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    goals = activity.add_mutually_exclusive_group()
    goals.add_argument("--own-goals", action="store_true")
    goals.add_argument("--no-own-goals", action="store_true")
    activity.add_argument("--daily-calls", type=int)
    tool_mode = activity.add_mutually_exclusive_group()
    tool_mode.add_argument(
        "--stream-tools", action="store_const", const=True, dest="stream_tools", default=None
    )
    tool_mode.add_argument(
        "--complete-tools", action="store_const", const=False, dest="stream_tools"
    )
    activity.add_argument("--cooldown-minutes", type=int)
    activity.add_argument("--own-goal-daily", type=int)
    activity.add_argument("--max-open-tasks", type=int)
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
        if arguments.command == "add-persona":
            asyncio.run(
                add_persona(
                    arguments.root,
                    persona_id=arguments.persona_id,
                    profile_markdown=read_profile(arguments.profile_file),
                )
            )
            print(
                "independent persona prepared; active runtime unchanged; no execution right granted"
            )
            return 0
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
        if arguments.command == "configure-http":
            configure_http_transport(arguments.root, keepalive_seconds=arguments.keepalive_seconds)
            print("HTTP keep-alive configured; restart serve to apply; agent stays STOPPED")
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
        if arguments.command == "configure-activity":
            changes = {}
            if arguments.stream_tools is not None:
                changes["stream_tools"] = arguments.stream_tools
            if arguments.enable or arguments.disable:
                changes["enabled"] = bool(arguments.enable)
            if arguments.own_goals or arguments.no_own_goals:
                changes["own_goals"] = bool(arguments.own_goals)
            for name in ("daily_calls", "cooldown_minutes", "own_goal_daily", "max_open_tasks"):
                if getattr(arguments, name) is not None:
                    changes[name] = getattr(arguments, name)
            current = asyncio.run(configure_activity(arguments.root, **changes))
            print(
                f"activity enabled={str(current.enabled).lower()}"
                f" daily_calls={current.daily_calls} cooldown_minutes={current.cooldown_minutes}"
                f" own_goals={str(current.own_goals).lower()}; agent stays STOPPED"
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
        if arguments.command == "enable-tickets":
            asyncio.run(enable_tickets(arguments.root))
            print("operator tickets enabled; no rights granted to the agent; agent stays STOPPED")
            return 0
        if arguments.command == "enable-workplace-files":
            limits = {
                key: value
                for key, value in (
                    ("max_file_bytes", arguments.max_file_bytes),
                    ("max_scope_bytes", arguments.max_scope_bytes),
                )
                if value is not None
            }
            asyncio.run(
                enable_workplace_files(
                    arguments.root,
                    blob_dir=arguments.blob_dir,
                    ceiling_days=arguments.ceiling_days,
                    **limits,
                )
            )
            print("workplace files enabled; no rights granted to the agent; agent stays STOPPED")
            return 0
        if arguments.command == "enable-work-sessions":
            limits = {
                name: getattr(arguments, name)
                for name in SESSION_LIMITS
                if getattr(arguments, name) is not None
            }
            asyncio.run(
                enable_work_sessions(
                    arguments.root,
                    image_id=arguments.image_id,
                    areas=arguments.areas,
                    output_area=arguments.output_area,
                    monitor_interval_seconds=arguments.monitor_interval_seconds,
                    prewarm_enabled=arguments.prewarm,
                    prewarm_idle_seconds=arguments.prewarm_idle_seconds,
                    ceiling_days=arguments.ceiling_days,
                    helper_enabled=arguments.helper,
                    helper_capability_id=arguments.helper_capability_id,
                    home_enabled=arguments.home,
                    egress_enabled=arguments.internet,
                    resident_enabled=arguments.resident,
                    wake_enabled=arguments.self_work,
                    wake_quiet_hours=arguments.quiet_hours,
                    egress_deny=arguments.egress_deny,
                    **{
                        **limits,
                        **{
                            name: getattr(arguments, name)
                            for name in HELPER_LIMITS
                            if getattr(arguments, name) is not None
                        },
                    },
                )
            )
            print("work conversations enabled; no rights granted to the agent; agent stays STOPPED")
            return 0
        if arguments.command == "check-work-sessions":
            prepared = load_config(paths(arguments.root)).work_sessions
            if prepared is None:
                raise HostError("WORK_SESSIONS_NOT_ENABLED")
            reason = check_work_sessions(prepared)
            if reason is not None:
                raise HostError(reason)
            print("work conversations ready: Docker and the pinned OpenCode image found")
            return 0
        if arguments.command == "configure-prompt-limit":
            configure_prompt_limit(
                arguments.root,
                max_prompt_chars=arguments.max_prompt_chars,
                max_output_tokens=arguments.max_output_tokens,
                timeout_seconds=arguments.timeout_seconds,
            )
            print("Live limits configured; run configure-activity to apply; stays STOPPED")
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
            opencode_ui_dir=arguments.opencode_ui_dir,
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
