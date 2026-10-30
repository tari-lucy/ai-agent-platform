"""Read-only launcher preflight: no serve, database, secrets, migrations or containers."""

import argparse
import json
import subprocess
from pathlib import Path

from ai_agent_core.local_host.layout import HostError, load_config, paths
from ai_agent_core.local_host.panel import validate
from ai_agent_core.local_host.work_sessions import check


def preflight(config: Path, checkout: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    required = {"python", "host_root", "provider", "env_file", "panel_dir"}
    try:
        if not config.is_file() or config.stat().st_size > 65536:
            raise ValueError()
        settings = json.loads(config.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(settings, dict)
            or not required <= settings.keys() <= required | {"opencode_ui_dir"}
            or not all(isinstance(value, str) for value in settings.values())
            or settings["provider"] not in {"mock", "live"}
        ):
            raise ValueError()
        for key in ("python", "host_root", "panel_dir"):
            if not Path(settings[key]).is_absolute():
                raise ValueError()
    except OSError, ValueError:
        return {"launcher": "LAUNCHER_CONFIG_INVALID"}
    results["launcher"] = "LAUNCHER_OK"
    # Probe the launcher's interpreter with -I, just as the real launcher does.
    # Never substitute PYTHONPATH and accidentally validate another editable checkout.
    probe = (
        "import json,sys,ai_agent_core; from pathlib import Path; "
        "print(json.dumps({'version':sys.version_info[:2],"
        "'source':str(Path(ai_agent_core.__file__).resolve())}))"
    )
    try:
        process = subprocess.run(
            [settings["python"], "-I", "-B", "-c", probe],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if process.returncode:
            raise ValueError()
        python = json.loads(process.stdout)
        expected = checkout / "apps/core/src/ai_agent_core/__init__.py"
        results["python"] = (
            "PYTHON_OK"
            if (
                python["version"] == [3, 14]
                and Path(python["source"]).resolve() == expected.resolve()
            )
            else "PYTHON_VERSION_OR_CHECKOUT_MISMATCH"
        )
    except OSError, ValueError, KeyError, subprocess.TimeoutExpired:
        results["python"] = "PYTHON_UNAVAILABLE"
    env = Path(settings["env_file"])
    results["env_file"] = (
        "ENV_FILE_OK"
        if (
            settings["provider"] == "live"
            and env.is_absolute()
            and env.is_file()
            or settings["provider"] == "mock"
            and settings["env_file"] == ""
        )
        else "ENV_FILE_PATH_INVALID"
    )  # Only existence; never open the file or check the key's value.
    host = Path(settings["host_root"])
    try:
        validate(Path(settings["panel_dir"]), host)
        results["panel"] = "PANEL_OK"
    except HostError as error:
        results["panel"] = error.code
    except OSError:
        results["panel"] = "PANEL_UNAVAILABLE"
    try:
        configured = load_config(paths(host)).work_sessions
        results["runner"] = (
            (check(configured) or "RUNNER_OK")
            if configured is not None
            else "WORK_SESSIONS_NOT_CONFIGURED"
        )
    except HostError as error:
        results["runner"] = error.code
    except OSError, ValueError:
        results["runner"] = "HOST_CONFIG_UNAVAILABLE"
    # opencode_ui_dir is accepted but ignored: the old /opencode/ window was removed.
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()
    checks = preflight(args.config, args.checkout)
    ok = all(code.endswith("_OK") for code in checks.values())
    print(json.dumps({"checks": checks, "technical_preflight_ok": ok}, indent=2))
    return 0 if ok else 78


if __name__ == "__main__":
    raise SystemExit(main())
