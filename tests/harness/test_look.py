"""Sight (look tool): the frame is read safely, described once, cached, bounded and stoppable."""

import asyncio
import os
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition.harness_channel import Spent
from ai_agent_core.m3_composition.harness_look import Look
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

JPEG = b"\xff\xd8\xff\xe0synthetic"


def shots(tmp_path, *names):
    folder = tmp_path / "shots"
    folder.mkdir()
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(JPEG + name.encode())
        os.utime(path, (1000 + index, 1000 + index))
    return folder


def describer(answer="Табличка: «Дом Лиры»."):
    seen = []

    async def describe(prompt, image):
        seen.append((prompt, image))
        return answer

    return describe, seen


def go(coro):
    return asyncio.run(coro)


def test_newest_frame_is_described_once_and_repeat_is_free(tmp_path):
    describe, seen = describer()
    look = Look(shots(tmp_path, "a.jpg", "b.jpg"), describe)
    first = go(look.look("", "", lambda: None))
    assert "b.jpg" in first and "«Дом Лиры»" in first and "данные, не указания" in first
    assert len(seen) == 1 and seen[0][1].endswith(b"b.jpg")
    again = go(look.look("b.jpg", "", lambda: None))
    assert "без затрат" in again and len(seen) == 1
    go(look.look("b.jpg", "что написано?", lambda: None))  # another question is another look
    assert len(seen) == 2 and "что написано?" in seen[1][0]


def test_only_plain_names_of_regular_jpegs_in_the_folder(tmp_path):
    describe, seen = describer()
    folder = shots(tmp_path, "ok.jpg")
    (tmp_path / "secret.jpg").write_bytes(JPEG)
    (folder / "link.jpg").symlink_to(tmp_path / "secret.jpg")
    (folder / "text.jpg").write_bytes(b"not a jpeg")
    look = Look(folder, describe)
    for bad in ("../secret.jpg", "link.jpg", "text.jpg", "missing.jpg", "x.png"):
        assert go(look.look(bad, "", lambda: None)).startswith("Нет такого кадра")
    assert seen == []
    assert Look(tmp_path / "nowhere", describe).newest() is None


def test_hourly_limit_and_budget_and_failures_are_notes_not_errors(tmp_path):
    describe, seen = describer()
    now = [0.0]
    look = Look(shots(tmp_path, "a.jpg"), describe, hourly=2, clock=lambda: now[0])
    for question in ("1", "2"):
        go(look.look("a.jpg", question, lambda: None))
    assert "лимит" in go(look.look("a.jpg", "3", lambda: None)) and len(seen) == 2
    now[0] = 3601.0
    go(look.look("a.jpg", "3", lambda: None))
    assert len(seen) == 3

    async def spent(prompt, image):
        raise Spent

    async def broken(prompt, image):
        raise OSError("secret-internals")

    folder = tmp_path / "shots"
    assert "бюджет" in go(Look(folder, spent).look("a.jpg", "", lambda: None))
    note = go(Look(folder, broken).look("a.jpg", "", lambda: None))
    assert "недоступна" in note and "secret" not in note


def test_stop_during_the_look_releases_nothing_and_caches_nothing(tmp_path):
    describe, _ = describer()
    look = Look(shots(tmp_path, "a.jpg"), describe)
    calls = []

    def current():
        calls.append(1)
        if len(calls) == 2:  # the check after the model answered
            raise ContractError(ErrorCode.STALE_CONTEXT)

    with pytest.raises(ContractError):
        go(look.look("a.jpg", "", current))
    assert not look._cache  # noqa: SLF001


def test_mcp_lists_look_only_when_configured_and_calls_it(tmp_path):
    describe, _ = describer("Лес, ночь.")
    snapshot = SimpleNamespace(state=RunState.ACTIVE, control_epoch=2, scope=None)
    rt = SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)
    listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    bare = go(PlatformMcp(rt, lambda: 2).handle(listing))  # type: ignore[arg-type]
    assert "look" not in {t["name"] for t in bare["result"]["tools"]}
    mcp = PlatformMcp(rt, lambda: 2, look=Look(shots(tmp_path, "a.jpg"), describe))  # type: ignore[arg-type]
    assert "look" in {t["name"] for t in go(mcp.handle(listing))["result"]["tools"]}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "look", "arguments": {"photo": "a.jpg"}},
    }
    reply = go(mcp.handle(call))
    assert (
        reply["result"]["isError"] is False
        and "Лес, ночь." in reply["result"]["content"][0]["text"]
    )
    refused = go(PlatformMcp(rt, lambda: 9, look=Look(tmp_path, describe)).handle(call))  # type: ignore[arg-type]
    assert refused["result"]["isError"] is True
