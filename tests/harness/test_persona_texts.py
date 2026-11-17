"""The panel text editor touches only a whitelist, keeps history and refuses stale saves."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_core.m3_composition.persona_texts import GUIDE_ID, TextStore

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="p-work", memory_mode="work")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def store(tmp_path: Path) -> tuple[TextStore, Path]:
    folder = tmp_path / "files" / "p"
    (folder / "playbook").mkdir(parents=True)
    (folder / "servers").mkdir()
    (folder / "GOALS.md").write_text("# Цели\nстарые\n", encoding="utf-8")
    (folder / "playbook" / "live.md").write_text("стрим\n", encoding="utf-8")
    (folder / "playbook" / "script.py").write_text("print(1)\n", encoding="utf-8")
    (folder / "secret.md").write_text("не из списка\n", encoding="utf-8")
    settings = SimpleNamespace(guide_file=tmp_path / "guide" / "AGENTS.md", history_dir=tmp_path / "history")
    harness = SimpleNamespace(settings=settings, folders=lambda: folder, persona=lambda: "p")
    return TextStore(harness), folder


def code(error: pytest.ExceptionInfo[ContractError]) -> ErrorCode:
    return error.value.error.code


def test_the_listing_is_a_whitelist(tmp_path: Path) -> None:
    texts, _ = store(tmp_path)
    ids = {item.id for item in texts.listing(SCOPE, NOW).items}
    assert "file:GOALS.md" in ids and "file:playbook:live.md" in ids and GUIDE_ID in ids
    assert "file:playbook:script.py" not in ids and "file:secret.md" not in ids  # not whitelisted
    assert TextStore(None).listing(SCOPE, NOW).enabled is False


@pytest.mark.parametrize("bad", [
    "file:secret.md", "file:../GOALS.md", "file:playbook:../GOALS.md", "file:playbook:script.py",
    "file:other:live.md", "guide:../x", "../etc/passwd", "file:GOALS.md:extra:more",
])
def test_ids_outside_the_whitelist_are_refused(tmp_path: Path, bad: str) -> None:
    texts, _ = store(tmp_path)
    with pytest.raises(ContractError) as error:
        texts.read(SCOPE, bad)
    assert code(error) in (ErrorCode.INVALID_ARGUMENTS, ErrorCode.CAPABILITY_UNAVAILABLE)


def test_a_save_replaces_keeps_history_and_journals(tmp_path: Path) -> None:
    texts, folder = store(tmp_path)
    opened = texts.read(SCOPE, "file:GOALS.md")
    assert opened.content.startswith("# Цели") and len(opened.version) == 64 and opened.history == []
    saved = texts.write(SCOPE, "op-1", "file:GOALS.md", "# Цели\nновые\n", opened.version)
    assert (folder / "GOALS.md").read_text(encoding="utf-8") == "# Цели\nновые\n"
    assert saved.version != opened.version and len(saved.history) == 1
    old = texts.revision(SCOPE, saved.history[0].rev)
    assert old.content == "# Цели\nстарые\n"  # what was replaced can be read back
    journal = (tmp_path / "history" / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(journal[0])["id"] == "file:GOALS.md" and json.loads(journal[0])["operator"] == "op-1"
    assert not list(folder.glob(".part-*"))  # no temporary file is left


def test_a_stale_save_is_a_conflict_and_changes_nothing(tmp_path: Path) -> None:
    texts, folder = store(tmp_path)
    opened = texts.read(SCOPE, "file:GOALS.md")
    (folder / "GOALS.md").write_text("она дописала сама\n", encoding="utf-8")  # the agent edited it
    with pytest.raises(ContractError) as error:
        texts.write(SCOPE, "op-1", "file:GOALS.md", "моя версия", opened.version)
    assert code(error) is ErrorCode.VERSION_CONFLICT
    assert (folder / "GOALS.md").read_text(encoding="utf-8") == "она дописала сама\n"


def test_a_missing_file_is_created_only_from_a_new_version(tmp_path: Path) -> None:
    texts, _ = store(tmp_path)
    fresh = texts.read(SCOPE, GUIDE_ID)
    assert fresh.version == "new" and fresh.content == ""
    with pytest.raises(ContractError) as error:
        texts.write(SCOPE, "op-1", GUIDE_ID, "правила", "0" * 64)
    assert code(error) is ErrorCode.VERSION_CONFLICT
    created = texts.write(SCOPE, "op-1", GUIDE_ID, "правила", "new")
    assert created.content == "правила" and created.history == [] and len(created.version) == 64


def test_only_the_active_work_persona_is_writable_and_sizes_are_bounded(tmp_path: Path) -> None:
    texts, _ = store(tmp_path)
    version = texts.read(SCOPE, "file:GOALS.md").version
    other = SCOPE.model_copy(update={"persona_id": "q"})
    test_scope = SCOPE.model_copy(update={"memory_mode": "test"})
    for scope in (other, test_scope):
        with pytest.raises(ContractError) as error:
            texts.write(scope, "op", "file:GOALS.md", "x", version)
        assert code(error) is ErrorCode.ACCESS_DENIED
    for content in ("я" * 40_000, "a\x00b"):  # 80 KB in UTF-8; a NUL byte
        with pytest.raises(ContractError) as error:
            texts.write(SCOPE, "op", "file:GOALS.md", content, version)
        assert code(error) is ErrorCode.INVALID_ARGUMENTS


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    texts, folder = store(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("снаружи", encoding="utf-8")
    (folder / "README.md").symlink_to(target)
    with pytest.raises(ContractError) as error:
        texts.write(SCOPE, "op", "file:README.md", "перезапись", "new")
    assert code(error) is ErrorCode.INVALID_ARGUMENTS
    assert target.read_text(encoding="utf-8") == "снаружи"


def test_history_keeps_the_last_thirty(tmp_path: Path) -> None:
    texts, _ = store(tmp_path)
    version = texts.read(SCOPE, "file:GOALS.md").version
    for number in range(34):
        saved = texts.write(SCOPE, "op", "file:GOALS.md", f"версия {number}\n", version)
        version = saved.version
    assert len(saved.history) == 30
