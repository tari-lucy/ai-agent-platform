"""W2 home wired through the offline launcher: off by default, image label, operator guide.

Synthetic model and no Docker; the container mechanics are in test_home_docker/test_real_home.
"""

import asyncio
import json
import subprocess

from test_activity import WorkModel, prepared

from ai_agent_core.local_host import work_sessions
from ai_agent_core.local_host.layout import WorkSessionsConfig, load_config, paths
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import check, enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_home import home, volume

IMAGE = "sha256:" + "a" * 64
AREAS = ["projects", "inbox"]


def inspected(monkeypatch, labels):
    def run(args, **kwargs):
        info = [{"Id": IMAGE, "Os": "linux", "Config": {"Labels": labels}}]
        return subprocess.CompletedProcess(args, 0, json.dumps(info).encode(), b"")

    monkeypatch.setattr(work_sessions.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(work_sessions.subprocess, "run", run)


def test_home_needs_the_workspace_image_label(monkeypatch):
    base = dict(image_id=IMAGE, areas=("projects",), output_area="projects")
    plain = {work_sessions.LABEL: work_sessions.VERSION}
    inspected(monkeypatch, plain)
    assert check(WorkSessionsConfig.model_validate(base)) is None
    on = WorkSessionsConfig.model_validate(dict(base, home_enabled=True))
    assert not WorkSessionsConfig.model_validate(base).home_enabled
    assert check(on) == "OPENCODE_WORKSPACE_IMAGE_UNEXPECTED"
    inspected(monkeypatch, {**plain, work_sessions.HOME_LABEL: "1"})
    assert check(on) is None


def test_enable_with_home_wires_the_persona_volume_and_operator_guide(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
        )
        await enable_workplace_files(host.root)
        monkeypatch.setattr(work_sessions, "check", lambda settings, **unused: None)
        await enable_work_sessions(
            host.root, image_id=IMAGE, areas=AREAS, output_area="projects",
            home_enabled=True, home_quota_mb=256,
        )  # fmt: skip
        settings = load_config(paths(host.root)).work_sessions
        assert settings.home_enabled and settings.home_quota_mb == 256
        # Re-running without the flag keeps the prepared choice.
        await enable_work_sessions(host.root, image_id=IMAGE, areas=AREAS, output_area="projects")
        assert load_config(paths(host.root)).work_sessions.home_enabled
        guide = paths(host.root).guide
        guide.parent.mkdir()
        guide.write_text("Синтетическое правило оператора", encoding="utf-8")
        async with host.open():
            rt, scope = host.runtime, host.scope
            sessions = rt.session_harness.__self__
            assert sessions.home.quota_mb == 256 and sessions.home.rules == guide
            made = sessions._home(scope)
            assert made.volume == volume(scope) and made.rules == "Синтетическое правило оператора"
            assert "/workspace" in made.connections and made.quota_mb == 256
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            # The operator edits the rules: the next turn reads them fresh.
            guide.write_text("Обновлённое правило", encoding="utf-8")
            assert sessions._home(scope).rules == "Обновлённое правило"
            assert home(sessions.home, scope, "x").volume == made.volume

    asyncio.run(scenario())


def test_cli_dispatches_home_arguments(monkeypatch, tmp_path):
    from ai_agent_core.local_host import cli

    calls = []

    async def sessions(root, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli, "enable_work_sessions", sessions)
    argv = ["enable-work-sessions", "--root", str(tmp_path), "--image-id", IMAGE]
    argv += ["--area", "projects", "--output-area", "projects"]
    assert cli.main(argv) == 0 and calls[-1]["home_enabled"] is None
    assert cli.main([*argv, "--home", "--home-quota-mb", "512"]) == 0
    assert calls[-1]["home_enabled"] is True and calls[-1]["home_quota_mb"] == 512
