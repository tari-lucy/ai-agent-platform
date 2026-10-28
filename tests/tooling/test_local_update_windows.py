"""Opt-in real Windows HTTP/STOP/backup/restart, using a disposable mock host.

The candidate is prepared separately by the real candidate/prepare functions.
No Docker launch, personal host, credentials or paid providers are used.
"""

import asyncio
import concurrent.futures
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_updater", ROOT / "scripts/update_local.py")
assert spec and spec.loader
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not os.environ.get("AW_UPDATER_CANDIDATE"),
    reason="opt-in: prepared Windows candidate checkout required",
)


def test_real_stop_backup_launcher_switch_and_restart(tmp_path, monkeypatch):
    from ai_agent_application.laboratory.profile import REFERENCE_PROFILE

    from ai_agent_core.local_host.init import initialise
    from ai_agent_core.local_host.recovery import enable_recovery

    old = Path(sys.executable).resolve().parents[2]
    ready = Path(os.environ["AW_UPDATER_CANDIDATE"]).resolve()
    host = tmp_path / "synthetic-host"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async def prepare_host():
        await initialise(host, profile_markdown=REFERENCE_PROFILE, port=port, test_scopes=1)
        await enable_recovery(
            host, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )

    asyncio.run(prepare_host())
    config = tmp_path / "config/local.launcher.json"
    config.parent.mkdir()
    original = dict(
        python=sys.executable,
        host_root=str(host),
        provider="mock",
        env_file="",
        panel_dir=str(old / "apps/panel/dist"),
    )
    updater.write_launcher(config, original)
    monkeypatch.setattr(updater, "candidate", lambda *_: ready)
    monkeypatch.setattr(updater, "prepare", lambda *_: ready / ".venv/Scripts/python.exe")
    monkeypatch.setattr(updater.webbrowser, "open", lambda *_: True)
    native_run = updater.run

    def run(args, **kwargs):
        if any(str(arg).endswith("check_opencode_deployment.py") for arg in args):
            # This host intentionally has neither OpenCode nor a runner. Still validate
            # the candidate's real Python binding, launcher, panel and mock env boundary.
            probe = (
                "import runpy,json,sys; from pathlib import Path; "
                "m=runpy.run_path(sys.argv[1]); "
                "print(json.dumps(m['preflight'](Path(sys.argv[2]),Path(sys.argv[3]))))"
            )
            values = json.loads(
                native_run(
                    [
                        args[0],
                        "-I",
                        "-c",
                        probe,
                        str(ready / "scripts/check_opencode_deployment.py"),
                        str(
                            config.parent.parent
                            / "runtime/operations/updater"
                            / next(
                                p.name
                                for p in (tmp_path / "runtime/operations/updater").glob(
                                    "*.candidate.json"
                                )
                            )
                        ),
                        str(ready),
                    ]
                )
            )
            assert values.pop("runner") == "WORK_SESSIONS_NOT_CONFIGURED"
            assert values.pop("native_ui") == "OPENCODE_UI_NOT_CONFIGURED"
            assert all(value.endswith("_OK") for value in values.values())
            return "synthetic host has no native UI/runner; remaining preflight checks passed"
        return native_run(args, **kwargs)

    monkeypatch.setattr(updater, "run", run)
    processes = []
    native_popen = subprocess.Popen
    log = (tmp_path / "server.log").open("ab")

    def popen(args, **kwargs):
        if "ai_agent_core.local_host" in args and "serve" in args:
            kwargs.update(stdout=log, stderr=log)
            process = native_popen(args, **kwargs)
            processes.append(process)
            return process
        return native_popen(args, **kwargs)

    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    old_server = popen(
        [
            sys.executable,
            "-I",
            "-m",
            "ai_agent_core.local_host",
            "serve",
            "--root",
            str(host),
            "--provider",
            "mock",
            "--panel-dir",
            original["panel_dir"],
        ]
    )
    origin = f"http://127.0.0.1:{port}"

    def wait_until(predicate, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        pytest.fail("synthetic server/update timeout")

    def available():
        try:
            return httpx.get(origin + "/", timeout=1, trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    worker = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        wait_until(available)
        future = worker.submit(updater.update, config)
        wait_until(lambda: future.done() or (len(processes) == 2 and available()), seconds=90)
        if future.done():
            future.result()
            pytest.fail("update returned before the new server was checked")
        updated = json.loads(config.read_text())
        assert Path(updated["python"]).resolve() == ready / ".venv/Scripts/python.exe"
        assert old_server.poll() == 0
        assert list((tmp_path / "runtime/operations/updater").glob("*.rollback.json"))
        result = json.loads(
            next((tmp_path / "runtime/operations/updater").glob("*.result.json")).read_text()
        )
        assert result["backup_id"]
        password = host / "secrets/operator-password.txt"
        with httpx.Client(base_url=origin, trust_env=False) as http:
            assert (
                http.post(
                    "/api/v1/auth/login",
                    json={"password": password.read_text().strip()},
                    headers={"Origin": origin},
                ).status_code
                == 200
            )
            scope = http.get("/api/v1/auth/session").json()["scopes"][0]
            query = {
                k: scope[k]
                for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
            }
            assert (
                http.get("/api/v1/sessions", params=query).json()["items"][0]["state"] == "STOPPED"
            )
        updater.stop_server(host, port, password)
        assert future.result(timeout=20) == 0
        # A failure before readiness must not leave the redirector's real Python behind.
        with monkeypatch.context() as patch:

            def broken_readiness(*_, **__):
                raise ValueError("synthetic readiness failure")

            patch.setattr(updater.httpx, "get", broken_readiness)
            with pytest.raises(ValueError, match="synthetic readiness failure"):
                updater.launch(updated, config, original, port)
        assert updater.process_at(port) is None
        assert all(process.poll() is not None for process in processes)
        assert json.loads(config.read_text()) == original
    finally:
        if available():
            updater.stop_server(host, port, host / "secrets/operator-password.txt")
        worker.shutdown(wait=True)
        for process in processes:
            process.wait(timeout=20)
        log.close()
