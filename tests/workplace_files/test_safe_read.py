"""Real filesystem negative probes; no AW-00 DTO, host DB, provider or model."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ai_agent_adapters.workspace_files import safe_read
from ai_agent_adapters.workspace_files.safe_read import UnsafeBlobError, read_blob

DATA = b"A fictional field report."
KEY = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "blobs"
    directory.mkdir()
    (directory / KEY).write_bytes(DATA)
    return directory


def symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("This test account cannot create symlinks")


def test_exact_bytes_and_empty_file(root):
    assert read_blob(root, KEY, max_bytes=len(DATA)) == DATA
    empty = hashlib.sha256(b"").hexdigest()
    (root / empty).write_bytes(b"")
    assert read_blob(root, empty, max_bytes=0) == b""


@pytest.mark.parametrize("name", ["../outside", "a/b", "a\\b", "C:secret", ".env", "A" * 64])
def test_untrusted_names_never_reach_io(root, name, monkeypatch):
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("unexpected open"))
    with pytest.raises(UnsafeBlobError, match="BLOB_ID_INVALID"):
        read_blob(root, name, max_bytes=100)


def test_limit_and_integrity(root):
    with pytest.raises(UnsafeBlobError, match="BLOB_TOO_LARGE"):
        read_blob(root, KEY, max_bytes=len(DATA) - 1)
    (root / KEY).write_bytes(b"wrong")
    with pytest.raises(UnsafeBlobError, match="BLOB_INTEGRITY_FAILED"):
        read_blob(root, KEY, max_bytes=100)


@pytest.mark.parametrize("limit", [-1, True, 2.5])
def test_bad_limits(root, limit):
    with pytest.raises(UnsafeBlobError, match="BLOB_LIMIT_INVALID"):
        read_blob(root, KEY, max_bytes=limit)


def test_relative_root_and_parent_traversal(root):
    for directory in (Path("relative"), root / ".." / root.name):
        with pytest.raises(UnsafeBlobError, match="BLOB_ROOT_INVALID"):
            read_blob(directory, KEY, max_bytes=100)


def test_leaf_symlink_rejected_without_touching_target(root, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(DATA)
    (root / KEY).unlink()
    symlink(root / KEY, outside)
    with pytest.raises(UnsafeBlobError):
        read_blob(root, KEY, max_bytes=100)
    assert outside.read_bytes() == DATA


def test_hardlink_rejected(root, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(DATA)
    (root / KEY).unlink()
    os.link(outside, root / KEY)
    with pytest.raises(UnsafeBlobError, match="BLOB_NOT_REGULAR"):
        read_blob(root, KEY, max_bytes=100)
    assert outside.read_bytes() == DATA


@pytest.mark.parametrize("ancestor", [False, True])
def test_directory_redirection_rejected(root, tmp_path, ancestor):
    link = tmp_path / "redirect"
    symlink(link, root.parent if ancestor else root, directory=True)
    with pytest.raises(UnsafeBlobError):
        read_blob(link / root.name if ancestor else link, KEY, max_bytes=100)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX allows a concurrent writer")
def test_growth_after_initial_check_is_bounded(root, monkeypatch):
    original = safe_read._regular
    grew = []

    def append_after_check(fd, limit):
        original(fd, limit)
        if not grew:
            with (root / KEY).open("ab") as writer:
                writer.write(b"extra")
            grew.append(True)

    monkeypatch.setattr(safe_read, "_regular", append_after_check)
    with pytest.raises(UnsafeBlobError, match="BLOB_TOO_LARGE"):
        read_blob(root, KEY, max_bytes=len(DATA))


def test_non_file_and_missing_are_safe_errors(root):
    (root / KEY).unlink()
    (root / KEY).mkdir()
    with pytest.raises(UnsafeBlobError) as failure:
        read_blob(root, KEY, max_bytes=100)
    assert str(root) not in str(failure.value)
    with pytest.raises(UnsafeBlobError):
        read_blob(root, "0" * 64, max_bytes=100)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle pinning")
def test_windows_root_cannot_be_swapped_during_open(root, monkeypatch):
    from ai_agent_adapters.workspace_files import _windows

    original = _windows._Kernel.open
    blocked = []
    # Establish that the same account can rename it while it is not pinned.
    moved = root.with_name("unpinned")
    root.rename(moved)
    moved.rename(root)

    def try_swap(self, path, *, directory):
        if not directory:
            try:
                root.rename(root.with_name("moved"))
            except OSError:
                blocked.append(True)
            else:
                pytest.fail("Unpinned directory could be renamed")
        return original(self, path, directory=directory)

    monkeypatch.setattr(_windows._Kernel, "open", try_swap)
    assert read_blob(root, KEY, max_bytes=100) == DATA
    assert blocked == [True]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing mode")
def test_windows_concurrent_writer_is_blocked(root, monkeypatch):
    original = safe_read._regular
    blocked = []

    def try_write(fd, limit):
        original(fd, limit)
        with pytest.raises(PermissionError):
            (root / KEY).write_bytes(b"changed")
        blocked.append(True)

    monkeypatch.setattr(safe_read, "_regular", try_write)
    assert read_blob(root, KEY, max_bytes=100) == DATA
    assert blocked == [True, True]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction")
def test_windows_junction_ancestor_rejected(root, tmp_path):
    link = tmp_path / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(root.parent)],
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert made.returncode == 0
    try:
        with pytest.raises(UnsafeBlobError):
            read_blob(link / root.name, KEY, max_bytes=100)
    finally:
        link.rmdir()  # remove this junction only, never recurse into its target
    assert (root / KEY).read_bytes() == DATA


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX anchored descriptor")
def test_posix_replacement_does_not_redirect_open(root, tmp_path, monkeypatch):
    original = os.open
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / KEY).write_bytes(b"external content")
    swapped = []

    def race_open(path, flags, *args, **kwargs):
        if path == KEY and not swapped:
            root.rename(root.with_name("moved"))
            root.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, race_open})
    assert read_blob(root, KEY, max_bytes=100) == DATA
    assert swapped == [True]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO")
def test_posix_fifo_is_rejected_without_waiting_for_a_writer(root):
    (root / KEY).unlink()
    os.mkfifo(root / KEY)
    with pytest.raises(UnsafeBlobError, match="BLOB_NOT_REGULAR"):
        read_blob(root, KEY, max_bytes=100)
