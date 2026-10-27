"""Fail-closed host commands: no overwrite, incomplete init, unsafe secrets, live gate, rights."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from ai_agent_application.storage import Owner
from host_support import command, init

from ai_agent_core.local_host import rights


def serve(root, *arguments):
    return subprocess.run(
        command("serve", "--root", str(root), *arguments),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("guards") / "host"
    done = init(root, "--test-scopes", "2")
    assert done.returncode == 0, done.stderr
    return root, done


def test_init_output_has_no_secret_and_owner_only_files(prepared):
    root, done = prepared
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    assert password not in done.stdout + done.stderr
    assert (root / "INIT-UNFINISHED").exists() is False
    if sys.platform == "win32":
        from ai_agent_core.local_host.layout import windows_private

        assert windows_private(root / "secrets" / "operator-password.hash")
        assert windows_private(root / "secrets" / "operator-password.txt")
        return
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0, path.name
        assert path.stat().st_uid == os.getuid()


def test_init_never_overwrites(prepared, tmp_path):
    root, _ = prepared
    before = (root / "host.json").read_bytes()
    again = init(root)
    assert again.returncode == 78 and "ROOT_NOT_EMPTY" in again.stderr
    assert (root / "host.json").read_bytes() == before
    occupied = tmp_path / "taken"
    occupied.mkdir()
    (occupied / "note.txt").write_text("keep")
    assert init(occupied).returncode == 78
    assert (occupied / "note.txt").read_text() == "keep"
    relative = subprocess.run(
        command("init", "--root", "relative-host"),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert relative.returncode == 78 and not (tmp_path / "relative-host").exists()


def test_invalid_options_create_nothing(tmp_path):
    root = tmp_path / "host"
    assert init(root, "--test-scopes", "8").returncode == 78
    assert init(root, "--permission-days", "91").returncode == 78
    assert not root.exists()


def test_serve_refuses_unprepared_or_unfinished(prepared, tmp_path):
    root, _ = prepared
    assert "INIT_INCOMPLETE" in serve(tmp_path / "missing", "--provider", "mock").stderr
    unfinished = tmp_path / "unfinished"
    unfinished.mkdir()
    (unfinished / "host.json").write_text((root / "host.json").read_text())
    (unfinished / "INIT-UNFINISHED").write_text("x")
    refused = serve(unfinished, "--provider", "mock")
    assert refused.returncode == 78 and "INIT_INCOMPLETE" in refused.stderr


def test_live_provider_and_missing_mode_are_refused_before_opening(prepared):
    root, _ = prepared
    live = serve(root, "--provider", "live")
    assert live.returncode == 78 and "LIVE_NOT_PREPARED" in live.stderr
    assert serve(root).returncode == 2  # the mode must be named explicitly


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_serve_refuses_a_readable_secret(tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    os.chmod(root / "secrets" / "operator-password.hash", 0o644)
    refused = serve(root, "--provider", "mock")
    assert refused.returncode == 78 and "SECRET_PERMISSIONS" in refused.stderr


def test_import_has_no_effects(tmp_path):
    # -I ignores PYTHONPATH and the cwd; give it exactly this checkout's sources, and drop an
    # editable install's finder that would map the packages to another checkout first.
    root = Path(rights.__file__).resolve().parents[5]  # apps/core/src/ai_agent_core/local_host
    sources = sorted(str(path) for path in root.glob("*/*/src") if path.is_dir())
    code = (
        f"import sys; sys.path[:0] = {sources!r}; "
        "sys.meta_path[:] = [f for f in sys.meta_path "
        "if not getattr(f, '__module__', '').startswith('__editable__')]; "
        "import ai_agent_core.local_host.cli, ai_agent_core.local_host.serve"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, check=False
    )
    assert done.returncode == 0 and list(tmp_path.iterdir()) == []


def test_rights_matrix_is_narrow():
    for phase in (False, True):
        grants = rights.work_grants(preparing=phase) + rights.lab_scope_grants()
        assert not any(g.public_disclosure or g.owner is Owner.MODERATION for g in grants)
        operator = [g for g in grants if g.principal.role == "operator"]
        assert operator and not any(g.write for g in operator)
    runtime = rights.work_grants()
    assert not any(g.write for g in runtime if g.owner is Owner.WORKSPACE)
    assert any(g.write for g in rights.work_grants(preparing=True) if g.owner is Owner.WORKSPACE)
    assert not any(g.owner is Owner.LABORATORY for g in rights.operator_grants())


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DACL")
@pytest.mark.parametrize("parent", [False, True])
def test_serve_refuses_broadened_windows_secret_acl(prepared, parent):
    root, _ = prepared
    target = root / "secrets" if parent else root / "secrets" / "operator-password.hash"
    changed = subprocess.run(
        ["icacls", str(target), "/grant", "*S-1-1-0:R"], capture_output=True, check=False
    )
    assert changed.returncode == 0
    try:
        refused = serve(root, "--provider", "mock")
        assert refused.returncode == 78 and "SECRET_PERMISSIONS" in refused.stderr
    finally:
        restored = subprocess.run(
            ["icacls", str(target), "/remove:g", "*S-1-1-0"], capture_output=True, check=False
        )
        assert restored.returncode == 0


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_invalid_serve_port_is_refused_before_listening(prepared, port):
    root, _ = prepared
    refused = serve(root, "--provider", "mock", "--port", port)
    assert refused.returncode == 78 and "INVALID_OPTION" in refused.stderr
