"""Updater safety with real disposable Git/SQLite and a synthetic STOP transport."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("local_updater", ROOT / "scripts/update_local.py")
assert spec and spec.loader
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=" + root.as_posix(), "-C", str(root), *args],
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf8",
    ).strip()


def commit(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf8")
    git(root, "add", name)
    git(root, "commit", "-m", "synthetic change")


@pytest.fixture
def repositories(tmp_path):
    remote, source, active = (tmp_path / p for p in ("remote.git", "source", "active"))
    remote.mkdir()
    git(remote, "init", "--bare")
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Synthetic Test")
    git(source, "config", "user.email", "test@example.invalid")
    commit(source, "base.txt", "base")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "origin", "main")
    git(source, "clone", "-b", "main", str(remote), str(active))
    git(active, "config", "user.name", "Synthetic Test")
    git(active, "config", "user.email", "test@example.invalid")
    return source, active


def test_candidate_preserves_local_commits_and_original_checkout(repositories, tmp_path):
    source, active = repositories
    commit(active, "local.txt", "local fix")
    before = git(active, "rev-parse", "HEAD")
    commit(source, "incoming.txt", "upstream")
    git(source, "push", "origin", "main")
    target = tmp_path / "candidate"
    assert updater.candidate(active, target, "synthetic") == target
    assert (target / "local.txt").read_text() == "local fix"
    assert (target / "incoming.txt").read_text() == "upstream"
    assert git(active, "rev-parse", "HEAD") == before
    assert not (active / "incoming.txt").exists()


def test_no_change_and_dirty_checkout(repositories, tmp_path):
    _, active = repositories
    target = tmp_path / "candidate"
    assert updater.candidate(active, target, "noop") == active
    assert not target.exists()
    (active / "base.txt").write_text("unfinished work")
    with pytest.raises(updater.UpdateError, match="DIRTY"):
        updater.candidate(active, target, "dirty")
    assert (active / "base.txt").read_text() == "unfinished work"
    assert not target.exists()


def test_conflict_aborts_only_new_candidate(repositories, tmp_path):
    source, active = repositories
    commit(source, "base.txt", "remote change")
    git(source, "push", "origin", "main")
    commit(active, "base.txt", "local change")
    before = git(active, "rev-parse", "HEAD")
    target = tmp_path / "candidate"
    with pytest.raises(updater.UpdateError, match="CONFLICT"):
        updater.candidate(active, target, "conflict")
    assert git(active, "rev-parse", "HEAD") == before
    assert (active / "base.txt").read_text() == "local change"
    assert git(target, "status", "--porcelain") == ""
    assert git(target, "rev-parse", "HEAD") == before


def test_changed_images_or_schema_require_separate_release(repositories, tmp_path):
    source, active = repositories
    commit(source, "migrations/versions/new.py", "# synthetic migration")
    git(source, "push", "origin", "main")
    before = git(active, "rev-parse", "HEAD")
    with pytest.raises(updater.UpdateError, match="MANUAL_RELEASE"):
        updater.candidate(active, tmp_path / "candidate", "release")
    assert git(active, "rev-parse", "HEAD") == before


def test_jobs_checks_current_revision_and_never_writes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "platform.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE records(scope_id,kind,id,version,deleted);"
            "CREATE TABLE record_revisions(scope_id,kind,id,version,payload);"
            "INSERT INTO records VALUES('scope','Job','job',2,0);"
            "INSERT INTO record_revisions VALUES('scope','Job','job',1,"
            '\'{"status":"OUTCOME_UNKNOWN"}\');'
            "INSERT INTO record_revisions VALUES('scope','Job','job',2,"
            '\'{"status":"CANCELLED"}\');'
        )
    before = path.read_bytes()
    assert updater.settled_jobs(tmp_path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE record_revisions SET payload='{}' WHERE version=2")
    assert not updater.settled_jobs(tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command line API")
def test_exact_process_command(tmp_path):
    host = tmp_path / "host with spaces"
    assert updater.own_command(
        f'"C:/Python/python.exe" -I -m ai_agent_core.local_host serve --root "{host}"', host
    )
    assert not updater.own_command(f'python unrelated.py --root "{host}"', host)
    assert not updater.own_command(
        "python -m ai_agent_core.local_host serve --root C:/another-host", host
    )


def test_wrong_process_is_never_authenticated_or_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "process_at", lambda _: (42, "another program"))
    monkeypatch.setattr(updater, "own_command", lambda *_: False)
    with pytest.raises(updater.UpdateError, match="PORT"):
        updater.stop_server(tmp_path, 8765, tmp_path / "nonexistent-password")


class Function:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


@pytest.mark.parametrize("confirmed", [True, False])
def test_terminate_only_after_confirmed_stop_and_known_jobs(tmp_path, monkeypatch, confirmed):
    kernel = SimpleNamespace(
        **{
            name: Function(result)
            for name, result in {
                "OpenProcess": 12345678901,
                "TerminateProcess": 1,
                "WaitForSingleObject": 0,
                "CloseHandle": 1,
            }.items()
        }
    )
    monkeypatch.setattr(updater.ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(updater, "process_at", lambda _: (42, "synthetic"))
    monkeypatch.setattr(updater, "own_command", lambda *_: True)
    monkeypatch.setattr(updater, "settled_jobs", lambda _: confirmed)
    clock = iter(range(0, 500, 10))
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(updater.time, "sleep", lambda _: None)
    password = tmp_path / "password"
    password.write_text("synthetic-test-password")
    scope = dict(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("auth/session"):
            data = dict(scopes=[scope], csrf_token="synthetic")
        elif request.url.path.endswith("/sessions"):
            data = dict(
                items=[
                    dict(agent_id="a", scope=scope, state="STOPPED", unconfirmed_resource_ids=[])
                ]
            )
        else:
            data = {}
        return httpx.Response(200, json=data)

    client = httpx.Client(transport=httpx.MockTransport(respond), base_url="http://127.0.0.1:8765")
    monkeypatch.setattr(updater.httpx, "Client", lambda **_: client)
    if confirmed:
        updater.stop_server(tmp_path, 8765, password)
        assert len(kernel.TerminateProcess.calls) == 1
        assert calls.index("/api/v1/agents/a/stop") < len(calls) - 1
    else:
        with pytest.raises(updater.UpdateError, match="STOP"):
            updater.stop_server(tmp_path, 8765, password)
        assert not kernel.TerminateProcess.calls
    assert len(kernel.CloseHandle.calls) == 1


def test_failed_launcher_write_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / "launcher.json"
    path.write_text(json.dumps({"previous": "preserve"}))

    def reject(*_):
        raise PermissionError("synthetic")

    monkeypatch.setattr(Path, "replace", reject)
    with pytest.raises(PermissionError):
        updater.write_launcher(path, {"new": "candidate"})
    assert json.loads(path.read_text()) == {"previous": "preserve"}
    assert list(tmp_path.iterdir()) == [path]


def test_spawn_failure_rolls_back_launcher(tmp_path, monkeypatch):
    config = tmp_path / "launcher.json"
    previous = {"python": "previous"}
    settings = dict(python="new", host_root="unused", provider="mock", panel_dir="unused")
    updater.write_launcher(config, settings)

    def reject(*_):
        raise OSError("synthetic")

    monkeypatch.setattr(updater.subprocess, "Popen", reject)
    with pytest.raises(updater.UpdateError, match="STARTUP"):
        updater.launch(settings, config, previous, 8765)
    assert json.loads(config.read_text()) == previous


def test_startup_rejects_an_unrelated_listener(tmp_path, monkeypatch):
    config = tmp_path / "launcher.json"
    previous = {"python": "previous"}
    settings = dict(python="new", host_root=str(tmp_path), provider="mock", panel_dir="unused")
    updater.write_launcher(config, settings)
    stopped = []
    child = SimpleNamespace(
        pid=42, poll=lambda: None, terminate=lambda: stopped.append(True), wait=lambda **_: 0
    )
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *_: child)
    monkeypatch.setattr(updater.httpx, "get", lambda *_, **__: SimpleNamespace(status_code=200))
    monkeypatch.setattr(updater, "process_at", lambda _: (99, "unrelated"))
    with pytest.raises(updater.UpdateError, match="PORT"):
        updater.launch(settings, config, previous, 8765)
    assert stopped == [True]
    assert json.loads(config.read_text()) == previous


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locks")
def test_parallel_updater_cannot_acquire_lock(tmp_path):
    path = tmp_path / "updater.lock"
    with updater.update_lock(path):
        with pytest.raises(updater.UpdateError, match="LOCK"):
            with updater.update_lock(path):
                pytest.fail("parallel update acquired lock")
    with updater.update_lock(path):
        pass
