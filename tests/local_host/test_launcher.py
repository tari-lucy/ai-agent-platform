"""Exercise the real Windows launcher parser with a harmless argument-capturing interpreter."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows entry point")
ROOT = Path(__file__).resolve().parents[2]


def launch(config):
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts/start-panel.ps1"),
            "-Config",
            str(config),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_launcher_uses_explicit_paths_and_never_reads_binding_values(tmp_path):
    target = tmp_path / "paths with spaces"
    target.mkdir()
    interpreter = target / "capture.ps1"
    interpreter.write_text(
        "$args | ConvertTo-Json -Compress | "
        "Set-Content -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot 'arguments.json')\n"
        "exit 0\n",
        encoding="utf-8",
    )
    env = target / "binding.env"
    env.write_text("do-not-read-this-placeholder", encoding="utf-8")
    config = target / "launcher.json"
    value = dict(
        python=str(interpreter),
        host_root=str(target),
        provider="live",
        env_file=str(env),
        panel_dir=str(target),
    )
    config.write_text(json.dumps(value), encoding="utf-8")
    done = launch(config)
    assert done.returncode == 0, done.stderr
    args = json.loads((target / "arguments.json").read_text(encoding="utf-8-sig"))
    assert args == [
        "-I",
        "-m",
        "ai_agent_core.local_host",
        "serve",
        "--root",
        str(target),
        "--provider",
        "live",
        "--panel-dir",
        str(target),
        "--open-browser",
        "--env-file",
        str(env),
    ]
    assert "do-not-read-this-placeholder" not in done.stdout + done.stderr
    value["opencode_ui_dir"] = str(target)
    config.write_text(json.dumps(value), encoding="utf-8")
    added = launch(config)
    assert added.returncode == 0, added.stderr
    newer = json.loads((target / "arguments.json").read_text(encoding="utf-8-sig"))
    assert newer == args[:11] + ["--opencode-ui-dir", str(target)] + args[11:]
    assert "do-not-read-this-placeholder" not in added.stdout + added.stderr
    value["unexpected"] = "PRIVATE_MARKER"
    config.write_text(json.dumps(value), encoding="utf-8")
    refused = launch(config)
    assert refused.returncode == 78 and "LAUNCHER_CONFIG_INVALID" in refused.stdout
    assert "PRIVATE_MARKER" not in refused.stdout + refused.stderr
    missing = launch(target / "missing.json")
    assert missing.returncode == 78 and "LAUNCHER_CONFIG_MISSING" in missing.stdout
