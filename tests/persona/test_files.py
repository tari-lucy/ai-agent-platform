"""Markdown profile files: confined to one directory, bounded, never silently overwritten."""

import os
from pathlib import Path

import pytest
from ai_agent_adapters.persona import ProfileFiles
from ai_agent_application.persona import ProfileImportRejected, import_markdown
from ai_agent_contracts.codec import ContractError
from persona_support import REFERENCE


def test_read_valid_file_then_import(tmp_path):
    (tmp_path / "spark.md").write_text(REFERENCE, encoding="utf-8")
    text = ProfileFiles(tmp_path).read("spark.md")
    parsed, issues = import_markdown(text)
    assert issues == () and parsed is not None


def test_bom_and_crlf_files_import_identically(tmp_path):
    (tmp_path / "plain.md").write_bytes(REFERENCE.encode("utf-8"))
    (tmp_path / "windows.md").write_bytes(
        b"\xef\xbb\xbf" + REFERENCE.replace("\n", "\r\n").encode()
    )
    files = ProfileFiles(tmp_path)
    plain, _ = import_markdown(files.read("plain.md"))
    windows, _ = import_markdown(files.read("windows.md"))
    assert plain is not None and windows is not None and plain.digest == windows.digest


@pytest.mark.parametrize(
    "name", ["../x.md", "a/b.md", "..md", ".hidden.md", "x.txt", "x.md\x00", "", "a b.md", "é.md"]
)
def test_names_outside_the_plain_pattern_are_refused(tmp_path, name):
    with pytest.raises(ContractError):
        ProfileFiles(tmp_path).read(name)
    with pytest.raises(ContractError):
        ProfileFiles(tmp_path).export(name, REFERENCE)


def test_missing_root_or_file(tmp_path):
    with pytest.raises(ContractError):
        ProfileFiles(tmp_path / "absent")
    with pytest.raises(ContractError):
        ProfileFiles(tmp_path).read("absent.md")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("не профиль", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside / "secret.md", root / "link.md")
    except OSError:
        pytest.skip("symlink creation not permitted")
    with pytest.raises(ContractError):
        ProfileFiles(root).read("link.md")


def test_directory_oversize_and_bad_encoding_are_rejected_safely(tmp_path):
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "big.md").write_bytes(b"x" * 70_000)
    (tmp_path / "latin.md").write_bytes("Канон".encode("cp1251"))
    files = ProfileFiles(tmp_path)
    with pytest.raises(ContractError):
        files.read("dir.md")
    with pytest.raises(ProfileImportRejected) as big:
        files.read("big.md")
    assert big.value.issues[0].code == "FILE_REJECTED"
    with pytest.raises(ProfileImportRejected) as latin:
        files.read("latin.md")
    assert latin.value.issues[0].code == "INVALID_ENCODING"


def test_export_creates_once_and_never_overwrites(tmp_path):
    files = ProfileFiles(tmp_path)
    files.export("snapshot.md", REFERENCE)
    assert (tmp_path / "snapshot.md").read_text("utf-8") == REFERENCE
    with pytest.raises(ContractError):
        files.export("snapshot.md", "другое")
    assert (tmp_path / "snapshot.md").read_text("utf-8") == REFERENCE
    assert [p.name for p in Path(tmp_path).iterdir()] == ["snapshot.md"]  # no temp leftovers
    with pytest.raises(ContractError):
        files.export("huge.md", "я" * 40_000)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_named_pipe_does_not_block_import(tmp_path):
    os.mkfifo(tmp_path / "pipe.md")
    with pytest.raises(ProfileImportRejected) as rejected:
        ProfileFiles(tmp_path).read("pipe.md")
    assert rejected.value.issues[0].code == "FILE_REJECTED"
